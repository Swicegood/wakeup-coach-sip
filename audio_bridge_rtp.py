"""Audio bridge using RTP and External Media Channel."""

import asyncio
import json
import logging
import os
from typing import Optional

import numpy as np
from scipy import signal

from ari_client import ARIClient
from coach_prompts import FIRST_RESPONSE_INSTRUCTIONS, FIRST_USER_SCENARIO
from openai_client import OpenAIRealtimeClient
from rtp_server import RTPServer


class AudioBridgeRTP:
    """Bridges audio between Asterisk and OpenAI using External Media Channel."""

    def __init__(
        self,
        ari_client: ARIClient,
        openai_client: OpenAIRealtimeClient,
        logger: logging.Logger
    ):
        """
        Initialize audio bridge.

        Args:
            ari_client: ARI client instance
            openai_client: OpenAI client instance
            logger: Logger instance
        """
        self.ari = ari_client
        self.openai = openai_client
        self.logger = logger
        self.running = False
        self.phone_channel_id: Optional[str] = None
        self.media_channel_id: Optional[str] = None
        self.rtp_server: Optional[RTPServer] = None
        self.asterisk_rtp_host: Optional[str] = None
        self.asterisk_rtp_port: Optional[int] = None
        self.rtp_relay_host: Optional[str] = None
        self.rtp_relay_port: Optional[int] = None
        self.bridge_id: Optional[str] = None
        self.streaming_tasks: list[asyncio.Task] = []  # Store task references for cancellation

    async def start(self, phone_channel_id: str, prime_rtp: bool = False):
        """
        Start bridging audio for a channel.

        Args:
            phone_channel_id: Phone channel ID
            prime_rtp: If True, send a short burst of RTP silence before the test tone.
                This helps the external RTP relay "learn" the endpoint on callback calls.
        """
        self.phone_channel_id = phone_channel_id
        self.running = True

        self.logger.info(f"Starting RTP audio bridge for channel {phone_channel_id}")

        try:
            # Decide Asterisk external media format (sample rate) from env.
            # Prefer slin24 if available to avoid resampling.
            media_format = os.getenv("ASTERISK_EXTERNAL_MEDIA_FORMAT", "slin24").strip()
            if media_format == "slin24":
                asterisk_rate_hz = 24000
            elif media_format == "slin16":
                asterisk_rate_hz = 16000
            elif media_format == "slin":
                asterisk_rate_hz = 8000
            else:
                raise ValueError(f"Unsupported ASTERISK_EXTERNAL_MEDIA_FORMAT={media_format!r} (use slin/slin16/slin24)")

            # Choose RTP PT. Asterisk UnicastRTP tends to tolerate dynamic PTs; keep it consistent.
            # For 8k/16k there are static PTs, but using dynamic avoids mismatches.
            rtp_pt = int(os.getenv("ASTERISK_RTP_PAYLOAD_TYPE", "96"))

            # Start RTP server with fixed port (default: 18000, within forwarded range)
            rtp_port = int(os.getenv("RTP_PORT", "18000"))
            self.rtp_server = RTPServer(
                self.logger,
                port=rtp_port,
                asterisk_sample_rate_hz=asterisk_rate_hz,
                rtp_payload_type=rtp_pt,
            )
            await self.rtp_server.start()

            # Set up RTP audio callback
            self.rtp_server.on_audio_received = self._handle_rtp_audio

            # Use RTP relay for NAT traversal
            # The relay forwards RTP bidirectionally between Asterisk and this app
            rtp_relay_host = os.getenv("EXTERNAL_RTP_HOST", "10.0.10.91")
            rtp_relay_port = int(os.getenv("EXTERNAL_RTP_HOST_PORT", "30000"))
            
            # Asterisk ExternalMedia should connect to the relay
            external_host = f"{rtp_relay_host}:{rtp_relay_port}"
            
            self.logger.info(f"Using RTP relay: {external_host}")
            self.logger.info(f"RTP server listening locally on 0.0.0.0:{self.rtp_server.port}")
            self.logger.info(f"Asterisk ExternalMedia will connect to relay at {external_host}")
            
            # Store relay endpoint for sending RTP packets
            self.rtp_relay_host = rtp_relay_host
            self.rtp_relay_port = rtp_relay_port

            # Create external media channel pointing to RTP relay
            self.logger.info(f"Creating ExternalMedia channel with:")
            self.logger.info(f"  external_host: {external_host}")
            self.logger.info(f"  format: {media_format}")
            self.logger.info(f"  encapsulation: rtp")
            self.logger.info(f"  transport: udp")
            self.logger.info(f"  connection_type: client")
            self.logger.info(f"  direction: both")
            
            media_channel = await self.ari.create_external_media(
                external_host=external_host,
                format=media_format
            )
            self.media_channel_id = media_channel.get("id")

            self.logger.info(f"External media channel created: {self.media_channel_id}")
            self.logger.info(f"External media channel details: {media_channel}")

            # Get Asterisk's RTP address from channel variables
            channel_vars = media_channel.get("channelvars", {})
            self.asterisk_rtp_host = channel_vars.get("UNICASTRTP_LOCAL_ADDRESS")
            self.asterisk_rtp_port = int(channel_vars.get("UNICASTRTP_LOCAL_PORT", 0))

            if self.asterisk_rtp_host and self.asterisk_rtp_port:
                self.logger.info(f"Asterisk RTP endpoint: {self.asterisk_rtp_host}:{self.asterisk_rtp_port}")
            else:
                self.logger.warning("Could not determine Asterisk RTP endpoint from channel vars")

            # Create a bridge
            bridge = await self._create_bridge()
            self.bridge_id = bridge.get("id")

            # Add both channels to the bridge
            await self._add_channel_to_bridge(self.phone_channel_id)
            await self._add_channel_to_bridge(self.media_channel_id)

            self.logger.info(f"Channels bridged: phone={self.phone_channel_id}, media={self.media_channel_id}")

            # Wait for external media channel to go to "Up" state (max 5 seconds)
            # This ensures RTP is ready before we start sending audio
            for _ in range(50):  # 50 * 0.1s = 5 seconds max
                channel_info = await self.ari.get_channel(self.media_channel_id)
                media_state = channel_info.get("state", "Unknown")
                if media_state == "Up":
                    self.logger.info(f"External media channel is now Up - RTP ready")
                    break
                await asyncio.sleep(0.1)
            else:
                self.logger.warning(f"External media channel still in {media_state} state after 5 seconds - proceeding anyway")

            # Wait a bit for RTP path to fully establish after channel goes Up
            await asyncio.sleep(0.3)

            # Send priming packets to wake up the RTP path
            # Asterisk won't send us packets until we send it something first
            # On callbacks, send more priming packets to ensure relay learns new endpoint
            num_priming_packets = 10 if prime_rtp else 5
            priming_duration_ms = num_priming_packets * 20
            if prime_rtp:
                self.logger.info(f"Callback detected: sending {num_priming_packets} RTP priming packets ({priming_duration_ms}ms) to establish relay path...")
            else:
                self.logger.info(f"Sending {num_priming_packets} RTP priming packets ({priming_duration_ms}ms) to wake up bidirectional path...")
            
            silence_24k = b'\x00' * 960  # 20ms @ 24kHz PCM16
            for i in range(num_priming_packets):
                if self.rtp_server and self.rtp_relay_host:
                    self.rtp_server.send_audio(silence_24k, self.rtp_relay_host, self.rtp_relay_port)
                await asyncio.sleep(0.02)  # 20ms between packets
            
            # On callbacks, wait a bit longer for relay to learn new endpoint
            if prime_rtp:
                await asyncio.sleep(0.2)
            
            # Now wait for Asterisk to respond (this confirms bidirectional path)
            wait_timeout = 2.0 if prime_rtp else 1.5  # Longer timeout on callbacks
            wait_iterations = int(wait_timeout * 10)  # 0.1s per iteration
            self.logger.info(f"Waiting for first incoming RTP packet to confirm bidirectional path (max {wait_timeout}s)...")
            for _ in range(wait_iterations):
                if hasattr(self.rtp_server, 'first_packet_received') and self.rtp_server.first_packet_received:
                    self.logger.info("First RTP packet received - bidirectional path confirmed!")
                    break
                await asyncio.sleep(0.1)
            else:
                self.logger.warning(f"No incoming RTP after {wait_timeout}s - proceeding anyway (may have audio issues)")

            # Small delay to let path settle
            await asyncio.sleep(0.1)

            # Send a test tone FIRST to verify audio path
            # This ensures the audio path is working before we start the conversation
            await self._send_test_tone()
            
            # Small delay after test tone to ensure it completes before starting OpenAI audio
            await asyncio.sleep(0.1)

            # Send initial message to OpenAI
            await self._send_initial_message()

            # Start audio streaming tasks
            # Create tasks explicitly so we can cancel them later
            self.streaming_tasks = [
                asyncio.create_task(self._stream_openai_to_rtp()),
                asyncio.create_task(self._listen_for_openai_responses()),
                asyncio.create_task(self._rtp_keepalive()),
            ]
            
            # Wait for tasks to complete (they will run until self.running = False)
            try:
                await asyncio.gather(*self.streaming_tasks, return_exceptions=True)
            except Exception as e:
                self.logger.error(f"Error in audio streaming tasks: {e}", exc_info=True)
                raise

        except KeyboardInterrupt:
            self.logger.info("Audio bridge interrupted")
            raise
        except Exception as e:
            self.logger.error(f"Error in audio bridge: {e}", exc_info=True)
            await self.stop()
            raise

    async def stop(self):
        """Stop the audio bridge."""
        self.logger.info("Stopping audio bridge")
        self.running = False

        # Cancel streaming tasks immediately
        if self.streaming_tasks:
            self.logger.info(f"Cancelling {len(self.streaming_tasks)} streaming tasks...")
            for task in self.streaming_tasks:
                if not task.done():
                    task.cancel()
            # Wait for tasks to be cancelled (with timeout)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.streaming_tasks, return_exceptions=True),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                self.logger.warning("Some streaming tasks didn't cancel within timeout")
            except Exception as e:
                self.logger.debug(f"Tasks cancelled: {e}")
            self.streaming_tasks = []

        # Stop RTP server
        if self.rtp_server:
            await self.rtp_server.stop()

        # Clean up bridge
        if self.bridge_id:
            try:
                url = f"{self.ari.config.base_url}/bridges/{self.bridge_id}"
                async with self.ari.session.delete(url) as resp:
                    if resp.status == 204:
                        self.logger.info(f"Bridge destroyed: {self.bridge_id}")
            except Exception as e:
                self.logger.error(f"Error destroying bridge: {e}")

        # Hangup external media channel
        if self.media_channel_id:
            try:
                await self.ari.hangup_channel(self.media_channel_id)
            except Exception as e:
                self.logger.error(f"Error hanging up media channel: {e}")

    async def _create_bridge(self):
        """Create a mixing bridge."""
        url = f"{self.ari.config.base_url}/bridges"
        payload = {
            "type": "mixing",
            "name": "wakeup-coach-bridge"
        }

        async with self.ari.session.post(url, json=payload) as resp:
            if resp.status == 200:
                bridge = await resp.json()
                self.logger.info(f"Bridge created: {bridge.get('id')}")
                return bridge
            else:
                error = await resp.text()
                raise RuntimeError(f"Failed to create bridge: {error}")

    async def _add_channel_to_bridge(self, channel_id: str):
        """Add a channel to the bridge."""
        url = f"{self.ari.config.base_url}/bridges/{self.bridge_id}/addChannel"
        params = {"channel": channel_id}

        async with self.ari.session.post(url, params=params) as resp:
            if resp.status == 204:
                self.logger.info(f"Channel {channel_id} added to bridge")
            else:
                error = await resp.text()
                raise RuntimeError(f"Failed to add channel to bridge: {error}")

    async def _send_test_tone(self):
        """Send a 440Hz test tone for 3 seconds to verify audio path."""
        import math
        import struct

        self.logger.info("Sending 440Hz test tone for 3 seconds...")

        sample_rate = 24000  # 24kHz (will be resampled to target rate)
        frequency = 440  # A4 note
        amplitude = 8000  # Moderate volume
        duration = 3  # seconds
        samples_per_packet = 480  # 20ms at 24kHz (standard ptime)

        total_packets = (sample_rate * duration) // samples_per_packet

        for packet_num in range(total_packets):
            # Generate samples for this packet
            samples = []
            for i in range(samples_per_packet):
                sample_index = packet_num * samples_per_packet + i
                t = sample_index / sample_rate
                value = int(amplitude * math.sin(2 * math.pi * frequency * t))
                samples.append(value)

            # Convert to little-endian bytes
            audio_data = struct.pack(f'<{len(samples)}h', *samples)

            # Send via RTP relay (will be resampled to target rate inside send_audio)
            if self.rtp_server and self.rtp_relay_host:
                self.rtp_server.send_audio(audio_data, self.rtp_relay_host, self.rtp_relay_port)

            # Wait 20ms between packets (standard ptime)
            await asyncio.sleep(0.02)

        self.logger.info("Test tone complete!")

    async def _send_initial_message(self):
        """Send initial greeting to OpenAI."""
        self.logger.info("Sending initial message to OpenAI")

        if self.openai.ws:
            initial_message = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": FIRST_USER_SCENARIO
                    }]
                }
            }

            await self.openai.ws.send(json.dumps(initial_message))

            # Trigger a response
            response_create = {
                "type": "response.create",
                "response": {
                    "output_modalities": ["audio"],
                    "instructions": FIRST_RESPONSE_INSTRUCTIONS,
                }
            }
            await self.openai.ws.send(json.dumps(response_create))

            self.logger.info("Initial message sent, waiting for OpenAI response")

    def _handle_rtp_audio(self, audio_data: bytes, remote_addr: tuple):
        """
        Handle audio received from RTP (phone).

        Args:
            audio_data: PCM16 audio from phone (8kHz)
            remote_addr: (host, port) tuple of sender
        """
        # Set Asterisk RTP address from first packet
        if not self.asterisk_rtp_host:
            self.asterisk_rtp_host, self.asterisk_rtp_port = remote_addr
            self.logger.info(f"Asterisk RTP address detected from packet: {self.asterisk_rtp_host}:{self.asterisk_rtp_port}")

        if not hasattr(self, '_rtp_packets_received'):
            self._rtp_packets_received = 0

        self._rtp_packets_received += 1
        if self._rtp_packets_received <= 3 or self._rtp_packets_received % 100 == 0:
            self.logger.info(f"_handle_rtp_audio called: packet #{self._rtp_packets_received}, {len(audio_data)} bytes from {remote_addr}")

        # Resample from 8kHz (Asterisk) to 24kHz (OpenAI)
        # audio_data is 8kHz PCM16, OpenAI expects 24kHz PCM16
        # Convert bytes to numpy array (int16 samples)
        audio_8k = np.frombuffer(audio_data, dtype=np.int16)
        
        if self._rtp_packets_received <= 3:
            # Log audio statistics to diagnose quality issues
            audio_min = np.min(audio_8k)
            audio_max = np.max(audio_8k)
            audio_mean = np.mean(np.abs(audio_8k))
            self.logger.info(f"Audio stats (8kHz): {len(audio_8k)} samples, min={audio_min}, max={audio_max}, mean_abs={audio_mean:.1f}")
        
        # Use resample_poly for better quality 3x upsampling (upsample by 3, downsample by 1)
        # This is better than signal.resample for integer ratios
        # Convert to float64 first (resample_poly requires float input)
        audio_8k_float = audio_8k.astype(np.float64)
        audio_24k_float = signal.resample_poly(audio_8k_float, 3, 1)  # 3x upsampling
        audio_24k = np.clip(audio_24k_float, -32768, 32767).astype(np.int16)
        
        if self._rtp_packets_received <= 3:
            audio_24k_min = np.min(audio_24k)
            audio_24k_max = np.max(audio_24k)
            audio_24k_mean = np.mean(np.abs(audio_24k))
            self.logger.info(f"Audio stats (24kHz resampled): {len(audio_24k)} samples, min={audio_24k_min}, max={audio_24k_max}, mean_abs={audio_24k_mean:.1f}")
        
        # Convert back to bytes
        audio_24k_bytes = audio_24k.tobytes()

        if self._rtp_packets_received <= 3:
            self.logger.info(f"Resampled {len(audio_8k)} samples (8kHz) -> {len(audio_24k)} samples (24kHz), {len(audio_24k_bytes)} bytes, sending to OpenAI")

        # Send to OpenAI
        if self.openai.ws and self.running:
            try:
                asyncio.create_task(self.openai.send_audio(audio_24k_bytes))
                if self._rtp_packets_received <= 3:
                    self.logger.info(f"Audio sent to OpenAI (task created)")
            except Exception as e:
                self.logger.error(f"Error sending audio to OpenAI: {e}", exc_info=True)
        else:
            if self._rtp_packets_received <= 3:
                self.logger.warning(f"Cannot send to OpenAI: ws={self.openai.ws is not None}, running={self.running}")

    async def _stream_openai_to_rtp(self):
        """Stream audio from OpenAI to RTP server."""
        self.logger.info("Starting OpenAI to RTP streaming")
        packets_sent = 0
        import time
        import struct
        
        # Target: send 50 packets per second (20ms per packet - standard for telephony)
        # For 8kHz: 20ms = 160 samples = 320 bytes
        # For 16kHz: 20ms = 320 samples = 640 bytes  
        # For 24kHz: 20ms = 480 samples = 960 bytes
        target_packet_interval = 0.02  # 20ms (standard ptime)
        next_packet_time = time.time()

        while self.running:
            try:
                # Wait until it's time for the next packet to maintain proper timing
                current_time = time.time()
                sleep_time = next_packet_time - current_time
                if sleep_time > 0:
                    await asyncio.sleep(min(sleep_time, 0.02))  # Cap sleep at 20ms to avoid long delays
                elif sleep_time < -0.1:
                    # We're way behind, reset timing
                    next_packet_time = current_time
                    self.logger.warning(f"Timing reset: {sleep_time*1000:.1f}ms behind")
                
                chunk = None
                buffer_size = 0
                
                # Check if we have audio from OpenAI
                async with self.openai.audio_lock:
                    buffer_size = len(self.openai.audio_buffer)

                    # Log buffer size periodically
                    if packets_sent % 100 == 0 and buffer_size > 0:
                        self.logger.info(f"OpenAI audio buffer: {buffer_size} bytes, packets sent: {packets_sent}")

                    # Standard frame size: 20ms
                    # For 8kHz: 20ms = 160 samples = 320 bytes
                    # For 16kHz: 20ms = 320 samples = 640 bytes
                    # For 24kHz: 20ms = 480 samples = 960 bytes
                    # We receive 24kHz from OpenAI, so need 960 bytes for 20ms
                    chunk_size_24k = 960  # 480 samples at 24kHz = 20ms
                    
                    if buffer_size >= chunk_size_24k:
                        # Extract chunk (480 samples at 24kHz = 20ms)
                        chunk = bytes(self.openai.audio_buffer[:chunk_size_24k])
                        del self.openai.audio_buffer[:chunk_size_24k]
                    elif buffer_size > 0:
                        # Pad with zeros to make full packet (better than partial)
                        chunk = bytes(self.openai.audio_buffer[:buffer_size])
                        del self.openai.audio_buffer[:buffer_size]
                        # Pad to 960 bytes with silence
                        padding_needed = chunk_size_24k - len(chunk)
                        if padding_needed > 0:
                            chunk += b'\x00' * padding_needed
                        self.logger.debug(f"Padded packet: {buffer_size} -> {chunk_size_24k} bytes")
                    else:
                        # No data - send silence to maintain timing
                        chunk = b'\x00' * chunk_size_24k  # 480 samples of silence at 24kHz

                # Check if we should still be running
                if not self.running:
                    self.logger.info("Streaming task stopping (running=False)")
                    break

                # Always send a packet to maintain timing, even if it's silence
                # Send to RTP relay, which forwards to Asterisk
                if self.rtp_server and self.rtp_relay_host:
                    packets_sent += 1
                    if packets_sent <= 5 or packets_sent % 50 == 0:
                        num_samples = len(chunk) // 2
                        duration_ms = (num_samples / 24000) * 1000
                        is_silence = all(b == 0 for b in chunk[:100])  # Check first 100 bytes
                        silence_marker = " (silence)" if is_silence else ""
                        self.logger.info(f"Sending OpenAI RTP packet #{packets_sent} to relay: {len(chunk)} bytes ({num_samples} samples, {duration_ms:.1f}ms@24kHz){silence_marker}")

                    # Record time before sending
                    send_start = time.time()
                    
                    # Send packet to relay - relay forwards to Asterisk
                    # The socket is non-blocking so this should return immediately
                    self.rtp_server.send_audio(
                        chunk,
                        self.rtp_relay_host,
                        self.rtp_relay_port
                    )
                    
                    send_duration = time.time() - send_start
                    if send_duration > 0.005 and packets_sent <= 10:
                        self.logger.warning(f"Send took {send_duration*1000:.2f}ms (target: <1ms)")
                    
                    # CRITICAL: Always increment next_packet_time by exactly 10ms
                    # This maintains steady 100 packets/second rate
                    # Don't use current time - use the scheduled time to prevent drift
                    next_packet_time = next_packet_time + target_packet_interval
                    
                    # Only reset if we're catastrophically behind (>100ms)
                    # Otherwise, let the steady increment catch up naturally
                    current_time = time.time()
                    if next_packet_time < current_time - 0.1:
                        # Way behind - reset to current time
                        if packets_sent % 100 == 0:
                            self.logger.warning(f"Timing reset: {(current_time - next_packet_time)*1000:.1f}ms behind")
                        next_packet_time = current_time

            except asyncio.CancelledError:
                self.logger.info("OpenAI to RTP streaming task cancelled")
                break
            except Exception as e:
                self.logger.error(f"Error streaming to RTP: {e}", exc_info=True)
                if self.running:
                    await asyncio.sleep(0.1)
                else:
                    break

    async def _rtp_keepalive(self):
        """
        Send periodic silence RTP packets to maintain NAT mappings and relay endpoint learning.
        
        This is critical for WSL2 NAT traversal - the relay needs to learn our endpoint,
        and NAT mappings need to stay open for incoming packets.
        """
        self.logger.info("Starting RTP keepalive task")
        
        # Send keepalive every 5 seconds (more frequent than typical NAT timeout of 30-60s)
        keepalive_interval = 5.0
        silence_samples_24k = 480  # 20ms of silence at 24kHz
        silence_bytes = b'\x00' * (silence_samples_24k * 2)  # 960 bytes of silence
        
        keepalive_count = 0
        
        while self.running:
            try:
                await asyncio.sleep(keepalive_interval)
                
                if self.rtp_server and self.rtp_relay_host:
                    keepalive_count += 1
                    if keepalive_count <= 3 or keepalive_count % 20 == 0:
                        self.logger.info(f"RTP keepalive #{keepalive_count}: Sending silence packet to relay to maintain NAT mapping")
                    
                    # Send silence packet to relay
                    self.rtp_server.send_audio(
                        silence_bytes,
                        self.rtp_relay_host,
                        self.rtp_relay_port
                    )
                else:
                    if keepalive_count == 0:
                        self.logger.warning("RTP keepalive: RTP server or relay not configured, skipping")
                    await asyncio.sleep(keepalive_interval)
                    
            except asyncio.CancelledError:
                self.logger.info("RTP keepalive task cancelled")
                break
            except Exception as e:
                self.logger.error(f"Error in RTP keepalive: {e}", exc_info=True)
                if self.running:
                    await asyncio.sleep(keepalive_interval)
                else:
                    break

    async def _listen_for_openai_responses(self):
        """Listen for audio responses from OpenAI."""
        self.logger.info("Listening for OpenAI responses...")

        try:
            # This calls the OpenAI client's listener which processes websocket messages
            # and populates the audio buffer via _handle_audio_delta()
            await self.openai.listen_for_responses()
        except asyncio.CancelledError:
            self.logger.info("OpenAI listener task cancelled")
        except Exception as e:
            self.logger.error(f"⚠️ CRITICAL: Error in OpenAI listener: {e}", exc_info=True)
            self.logger.error(f"⚠️ This will cause the bridge to stop and the call to end!")
            # Don't re-raise - let the bridge handle it gracefully
            # The bridge will stop when self.running = False
