"""RTP server for bidirectional audio streaming with Asterisk External Media Channel."""

import asyncio
import logging
import socket
import struct
import audioop  # For G.711 ulaw/alaw decoding
from typing import Optional, Callable
import numpy as np
from scipy import signal


class RTPServer:
    """
    Simple RTP server for audio streaming.

    Receives RTP packets from Asterisk and sends RTP packets to Asterisk.
    """

    def __init__(
        self,
        logger: logging.Logger,
        port: int = 0,
        asterisk_sample_rate_hz: int = 24000,
        rtp_payload_type: int = 96,
    ):
        """
        Initialize RTP server.

        Args:
            logger: Logger instance
            port: UDP port to bind (0 = random available port)
            asterisk_sample_rate_hz: Sample rate Asterisk expects for externalMedia (e.g. 8000/16000/24000)
            rtp_payload_type: RTP PT value to put in outbound packets (dynamic PTs 96-127 are safest)
        """
        self.logger = logger
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.sequence = 0
        self.timestamp = 0
        self.ssrc = 12345  # Synchronization source identifier
        self.asterisk_sample_rate_hz = asterisk_sample_rate_hz
        self.rtp_payload_type = rtp_payload_type

        # Callbacks
        self.on_audio_received: Optional[Callable[[bytes, tuple], None]] = None

        # Flag to track if we've received the first packet (for relay endpoint learning)
        self.first_packet_received = False

    async def start(self):
        """Start the RTP server."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', self.port))
        self.sock.setblocking(False)

        # Get the actual port we bound to
        self.port = self.sock.getsockname()[1]

        self.running = True
        self.first_packet_received = False  # Reset for new call
        self.sequence = 0  # Reset sequence for new call
        self.timestamp = 0  # Reset timestamp for new call
        self.logger.info(f"RTP server started on port {self.port}")

        # Start receiving loop
        asyncio.create_task(self._receive_loop())

    async def stop(self):
        """Stop the RTP server."""
        self.running = False
        if self.sock:
            self.sock.close()
            self.sock = None
        self.logger.info("RTP server stopped")

    async def _receive_loop(self):
        """Receive RTP packets from Asterisk."""
        loop = asyncio.get_event_loop()
        packets_received = 0
        last_ts: Optional[int] = None
        last_seq: Optional[int] = None

        self.logger.info("RTP receive loop started")

        while self.running:
            try:
                # Receive packet with timeout to allow periodic logging
                try:
                    data, addr = await asyncio.wait_for(
                        loop.sock_recvfrom(self.sock, 2048),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    # Log periodically if no packets received
                    if packets_received == 0:
                        # Log less frequently to avoid spam
                        import time
                        if not hasattr(self, '_last_warning_time'):
                            self._last_warning_time = 0
                        now = time.time()
                        if now - self._last_warning_time > 10.0:  # Log every 10 seconds
                            self.logger.warning(
                                f"RTP receive loop: No packets received yet (waiting for Asterisk to send RTP). "
                                f"Listening on 0.0.0.0:{self.port}. "
                                f"Check: 1) Port forwarding active? 2) Firewall allows UDP? 3) Asterisk can reach EXTERNAL_RTP_HOST?"
                            )
                            self._last_warning_time = now
                    continue
                
                packets_received += 1

                # Log first packet with full details and set flag
                if packets_received == 1:
                    self.first_packet_received = True  # Signal that relay has learned our endpoint
                    self.logger.info(f"FIRST RTP packet received from relay! {len(data)} bytes from {addr}. This confirms relay is forwarding packets!")
                
                # Log first few packets and every 100th packet
                if packets_received <= 3 or packets_received % 100 == 0:
                    self.logger.info(f"RTP received packet #{packets_received}: {len(data)} bytes from {addr}")

                if len(data) < 12:
                    self.logger.warning(f"Invalid RTP packet: too short ({len(data)} bytes)")
                    continue  # Invalid RTP packet

                # Parse RTP header (simplified)
                # Format: V(2)P(1)X(1)CC(4) M(1)PT(7) Sequence(16) Timestamp(32) SSRC(32)
                header = struct.unpack('!BBHII', data[:12])
                payload = data[12:]

                if packets_received <= 3:
                    pt = header[1] & 0x7F
                    marker = (header[1] & 0x80) >> 7
                    self.logger.info(
                        f"RTP header from relay: seq={header[2]}, ts={header[3]}, pt={pt}, m={marker}, payload={len(payload)} bytes, from={addr}"
                    )

                # Periodic sanity: report clocking/packetization that Asterisk is sending us
                if packets_received <= 5 or packets_received % 200 == 0:
                    pt = header[1] & 0x7F
                    seq = header[2]
                    ts = header[3]
                    if last_ts is not None and last_seq is not None:
                        dseq = (seq - last_seq) & 0xFFFF
                        dts = (ts - last_ts) & 0xFFFFFFFF
                        self.logger.info(
                            f"RTP from relay (Asterisk→relay→app): pt={pt}, payload={len(payload)} bytes, dseq={dseq}, dts={dts}"
                        )
                    last_ts = ts
                    last_seq = seq

                # Decode audio based on payload type
                if payload:
                    pt = header[1] & 0x7F
                    
                    if pt == 0:  # G.711 ulaw
                        # Decode ulaw to PCM16 using audioop (standard library, accurate)
                        if packets_received == 1:
                            self.logger.warning(f"Received ulaw (PT=0) but expected PCM16! Decoding ulaw to PCM16 using audioop.")
                        
                        # Use audioop.ulaw2lin for accurate G.711 ulaw decoding
                        # audioop.ulaw2lin(data, width) where width=2 means 16-bit samples
                        audio_le = audioop.ulaw2lin(payload, 2)
                        audio_pcm = np.frombuffer(audio_le, dtype=np.int16)
                        
                        if packets_received <= 3:
                            self.logger.info(f"Decoded ulaw: {len(payload)} bytes -> {len(audio_le)} bytes PCM16, min={audio_pcm.min()}, max={audio_pcm.max()}, mean_abs={np.mean(np.abs(audio_pcm)):.1f}")
                        
                    elif pt == 8:  # G.711 alaw
                        if packets_received <= 3:
                            self.logger.warning(f"Received alaw (PT=8) but expected PCM16! Decoding alaw to PCM16.")
                        
                        # alaw decoding
                        alaw_bytes = np.frombuffer(payload, dtype=np.uint8)
                        # alaw decode formula
                        sign = (alaw_bytes & 0x80) >> 7
                        exponent = (alaw_bytes & 0x70) >> 4
                        mantissa = alaw_bytes & 0x0F
                        
                        # Reconstruct linear value
                        linear = ((mantissa << 4) | 0x08) << (exponent - 1) if exponent != 0 else mantissa << 4
                        if sign == 0:
                            linear = -linear
                        
                        audio_pcm = linear.astype(np.int16)
                        audio_le = audio_pcm.tobytes()
                        
                    elif pt == 10 or pt == 11:  # L16 (PCM16) at 8kHz or 16kHz
                        # Convert from big-endian (Asterisk L16 network order) to little-endian
                        audio_be = np.frombuffer(payload, dtype=np.dtype('>i2'))  # Big-endian int16
                        audio_le = audio_be.astype(np.int16).tobytes()  # Native byte order (little-endian on x86)
                    else:
                        # Unknown payload type - try to decode as PCM16 big-endian
                        if packets_received <= 3:
                            self.logger.warning(f"Unknown RTP payload type {pt}, attempting PCM16 decode")
                        audio_be = np.frombuffer(payload, dtype=np.dtype('>i2'))
                        audio_le = audio_be.astype(np.int16).tobytes()

                    # Call callback with decoded audio payload
                    if self.on_audio_received:
                        if packets_received <= 3:
                            self.logger.info(f"Calling audio callback with {len(audio_le)} bytes (from {len(payload)} byte payload, PT={pt})")
                        try:
                            self.on_audio_received(audio_le, addr)
                        except Exception as e:
                            self.logger.error(f"Error in audio callback: {e}", exc_info=True)
                    elif packets_received == 1:
                        self.logger.warning("No audio callback registered - audio will not be sent to OpenAI!")

            except BlockingIOError:
                await asyncio.sleep(0.01)
            except Exception as e:
                self.logger.error(f"Error receiving RTP: {e}")
                await asyncio.sleep(0.1)

    async def send_audio_16k(self, audio_data: bytes, remote_host: str, remote_port: int) -> int:
        """
        Send audio that's already at 16kHz as RTP packet to Asterisk.

        Args:
            audio_data: PCM16 audio data at 16kHz (little-endian)
            remote_host: Asterisk IP
            remote_port: Asterisk RTP port

        Returns:
            Number of 16kHz samples sent
        """
        if not self.sock:
            self.logger.error("Cannot send RTP: socket not initialized")
            return 0

        # Audio is already 16kHz, just convert endianness
        num_samples = len(audio_data) // 2
        audio_16k = np.frombuffer(audio_data, dtype=np.int16)

        # Convert to big-endian for L16 payload
        audio_be = struct.pack(f'>{len(audio_16k)}h', *audio_16k)

        # Build RTP header
        header = struct.pack('!BBHII',
            0x80,  # V=2, P=0, X=0, CC=0
            self.rtp_payload_type,  # M=0, PT
            self.sequence,
            self.timestamp,
            self.ssrc
        )

        # Send packet
        packet = header + audio_be
        try:
            bytes_sent = self.sock.sendto(packet, (remote_host, remote_port))

            if self.sequence <= 3 or self.sequence % 100 == 0:
                self.logger.info(f"RTP packet sent (16kHz native): seq={self.sequence}, ts={self.timestamp}, samples={len(audio_16k)}, bytes={bytes_sent}, dest={remote_host}:{remote_port}")

            # Update sequence and timestamp
            self.sequence = (self.sequence + 1) % 65536
            self.timestamp += len(audio_16k)

            return len(audio_16k)

        except Exception as e:
            self.logger.error(f"Error sending RTP: {e}")
            return 0

    def send_audio(self, audio_data: bytes, remote_host: str, remote_port: int) -> int:
        """
        Send audio as RTP packet to Asterisk.

        Args:
            audio_data: PCM16 audio data at 24kHz (little-endian from OpenAI)
            remote_host: Asterisk IP
            remote_port: Asterisk RTP port

        Returns:
            Number of samples sent at Asterisk's configured sample rate
        """
        if not self.sock:
            self.logger.error("Cannot send RTP: socket not initialized")
            return 0

        # Convert OpenAI 24kHz -> Asterisk sample rate if needed.
        # OpenAI output is treated as 24kHz PCM16 mono.
        num_samples_24k = len(audio_data) // 2
        audio_24k = np.frombuffer(audio_data, dtype=np.int16)

        if self.asterisk_sample_rate_hz == 24000:
            audio_out = audio_24k
        elif self.asterisk_sample_rate_hz == 16000:
            # Resample from 24kHz to 16kHz: 16/24 = 2/3 ratio
            # resample_poly is better for integer ratios (upsample by 2, downsample by 3)
            # Convert to float64 first (resample_poly requires float input)
            audio_24k_float = audio_24k.astype(np.float64)
            audio_out_float = signal.resample_poly(audio_24k_float, 2, 3)  # 16/24 = 2/3
            audio_out_float = np.clip(audio_out_float, -32768, 32767)
            audio_out = audio_out_float.astype(np.int16)
            # Verify we got the expected number of samples
            expected_samples = int(num_samples_24k * 2 / 3)
            if len(audio_out) != expected_samples:
                self.logger.warning(f"Resampling produced {len(audio_out)} samples, expected {expected_samples}")
        elif self.asterisk_sample_rate_hz == 8000:
            # Convert to float64 first (resample_poly requires float input)
            audio_24k_float = audio_24k.astype(np.float64)
            audio_out_float = signal.resample_poly(audio_24k_float, 1, 3)  # 8/24
            audio_out_float = np.clip(audio_out_float, -32768, 32767)
            audio_out = audio_out_float.astype(np.int16)
        else:
            raise ValueError(f"Unsupported Asterisk sample rate: {self.asterisk_sample_rate_hz}")

        if self.sequence <= 100 and (self.sequence <= 5 or self.sequence % 20 == 0):
            duration_24k = num_samples_24k / 24000
            duration_out = len(audio_out) / float(self.asterisk_sample_rate_hz)
            self.logger.info(
                f"[Seq {self.sequence}] Resampled {num_samples_24k} samples ({duration_24k*1000:.1f}ms@24kHz) -> "
                f"{len(audio_out)} samples ({duration_out*1000:.1f}ms@{self.asterisk_sample_rate_hz}Hz), "
                f"min/max: {audio_out.min()}/{audio_out.max()}"
            )

        # Convert to big-endian for L16 payload
        audio_be = struct.pack(f'>{len(audio_out)}h', *audio_out)

        # Build RTP header
        # Version=2, Padding=0, Extension=0, CSRC count=0
        # Marker=0, Payload type is dynamic (Asterisk UnicastRTP is usually fine with this)
        header = struct.pack('!BBHII',
            0x80,  # V=2, P=0, X=0, CC=0
            self.rtp_payload_type,    # M=0, PT
            self.sequence,
            self.timestamp,
            self.ssrc
        )

        # Send packet with big-endian audio
        packet = header + audio_be
        try:
            # Use sendto which is non-blocking (socket is set to non-blocking)
            bytes_sent = self.sock.sendto(packet, (remote_host, remote_port))

            if self.sequence <= 3 or self.sequence % 100 == 0:
                self.logger.info(
                    f"RTP packet sent to relay (app→relay→Asterisk): seq={self.sequence}, ts={self.timestamp}, samples={len(audio_out)}, "
                    f"bytes={bytes_sent}, pt={self.rtp_payload_type}, dest={remote_host}:{remote_port}"
                )

            # Update sequence and timestamp
            self.sequence = (self.sequence + 1) % 65536
            self.timestamp += len(audio_out)

            return len(audio_out)

        except Exception as e:
            self.logger.error(f"Error sending RTP: {e}")
            return 0
