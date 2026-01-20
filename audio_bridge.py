"""Audio bridge between Asterisk and OpenAI."""

import asyncio
import json
import logging
import os
import tempfile
import wave
from typing import Optional
from aiohttp import web

from ari_client import ARIClient
from openai_client import OpenAIRealtimeClient
from config import HTTPConfig


class AudioBridge:
    """Bridges audio between Asterisk channel and OpenAI."""

    def __init__(
        self,
        ari_client: ARIClient,
        openai_client: OpenAIRealtimeClient,
        http_config: HTTPConfig,
        logger: logging.Logger
    ):
        """
        Initialize audio bridge.

        Args:
            ari_client: ARI client instance
            openai_client: OpenAI client instance
            http_config: HTTP server configuration
            logger: Logger instance
        """
        self.ari = ari_client
        self.openai = openai_client
        self.http_config = http_config
        self.logger = logger
        self.running = False
        self.channel_id: Optional[str] = None
        self.audio_buffer = bytearray()
        self.buffer_lock = asyncio.Lock()
        self.temp_dir = tempfile.mkdtemp(prefix="wakeup_audio_")
        self.http_app = None
        self.http_runner = None
        self.playing = False

    async def start(self, channel_id: str):
        """
        Start bridging audio for a channel.

        Args:
            channel_id: Channel ID to bridge
        """
        self.logger.info(f"Starting audio bridge for channel {channel_id}")
        self.running = True
        self.channel_id = channel_id

        try:
            # Start HTTP server for serving audio files
            await self._start_http_server()

            # Set up OpenAI audio callback
            async def audio_callback(audio_data):
                await self._buffer_audio_from_openai(audio_data)

            self.openai.on_audio_data = audio_callback

            # Send initial message to OpenAI to start conversation
            await self._send_initial_message()

            # Start tasks
            await asyncio.gather(
                self.openai.listen_for_responses(),
                self._play_buffered_audio()
            )

        except Exception as e:
            self.logger.error(f"Error in audio bridge: {e}", exc_info=True)
            raise
        finally:
            self.running = False
            await self._cleanup()

    async def _start_http_server(self):
        """Start HTTP server to serve audio files."""
        self.http_app = web.Application()
        self.http_app.router.add_get('/audio/{filename}', self._serve_audio_file)

        self.http_runner = web.AppRunner(self.http_app)
        await self.http_runner.setup()

        site = web.TCPSite(self.http_runner, '0.0.0.0', self.http_config.port)
        await site.start()

        self.logger.info(f"HTTP server started on {self.http_config.host}:{self.http_config.port}")

    async def _serve_audio_file(self, request):
        """Serve an audio file via HTTP."""
        filename = request.match_info['filename']
        filepath = os.path.join(self.temp_dir, filename)

        if not os.path.exists(filepath):
            return web.Response(status=404, text="File not found")

        return web.FileResponse(filepath)

    async def _send_initial_message(self):
        """Send initial greeting to OpenAI to start the conversation."""
        self.logger.info("Sending initial message to OpenAI")

        if self.openai.ws:
            # Create a conversation item with user message
            initial_message = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Hello! Good morning! Start the conversation."
                        }
                    ]
                }
            }
            await self.openai.ws.send(json.dumps(initial_message))

            # Trigger response generation
            await asyncio.sleep(0.1)
            response_create = {
                "type": "response.create"
            }
            await self.openai.ws.send(json.dumps(response_create))
            self.logger.info("Initial message sent, waiting for OpenAI response")

    async def _buffer_audio_from_openai(self, audio_data: bytes):
        """
        Buffer audio from OpenAI for playback.

        Args:
            audio_data: Audio data from OpenAI (PCM16)
        """
        async with self.buffer_lock:
            self.audio_buffer.extend(audio_data)
            # Log occasionally to avoid spam
            if len(self.audio_buffer) % 32000 < len(audio_data):
                self.logger.info(f"Audio buffer: {len(self.audio_buffer)} bytes")

    async def _play_buffered_audio(self):
        """Periodically check buffer and play audio chunks."""
        # Wait for initial buffering
        await asyncio.sleep(2)

        chunk_counter = 0

        while self.running:
            try:
                # Check if we have enough audio to play (about 2 seconds at 16kHz 16-bit)
                async with self.buffer_lock:
                    buffer_size = len(self.audio_buffer)

                if buffer_size >= 64000 and not self.playing:  # ~2 seconds of audio
                    self.playing = True

                    async with self.buffer_lock:
                        # Extract chunk
                        chunk_size = min(buffer_size, 64000)
                        chunk = bytes(self.audio_buffer[:chunk_size])
                        del self.audio_buffer[:chunk_size]
                        self.logger.info(f"Playing audio chunk: {chunk_size} bytes, {len(self.audio_buffer)} remaining")

                    # Create WAV file
                    chunk_counter += 1
                    filename = f"audio_{chunk_counter}.wav"
                    filepath = os.path.join(self.temp_dir, filename)

                    await self._create_wav_file(filepath, chunk)

                    # Play via ARI
                    await self._play_audio_file(filename)

                    # Clean up old file after a delay
                    asyncio.create_task(self._delete_file_after_delay(filepath, 30))

                    self.playing = False

                # Check buffer frequently
                await asyncio.sleep(0.5)

            except Exception as e:
                self.logger.error(f"Error in play loop: {e}", exc_info=True)
                self.playing = False
                await asyncio.sleep(1)

    async def _create_wav_file(self, filepath: str, audio_data: bytes):
        """
        Create a WAV file from PCM audio data.

        Args:
            filepath: Path to save WAV file
            audio_data: PCM16 audio data
        """
        self.logger.info(f"Creating WAV file: {filepath} ({len(audio_data)} bytes)")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_wav_sync, filepath, audio_data)
        self.logger.info(f"WAV file created: {filepath}")

    def _write_wav_sync(self, filepath: str, audio_data: bytes):
        """Synchronous WAV file writing."""
        with wave.open(filepath, 'wb') as wav:
            wav.setnchannels(1)  # Mono
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(16000)  # 16kHz
            wav.writeframes(audio_data)

    async def _play_audio_file(self, filename: str):
        """
        Play audio file via ARI.

        Args:
            filename: Name of the audio file to play
        """
        if not self.channel_id:
            return

        try:
            # TEMPORARY TEST: Use Asterisk built-in sound to verify playback works
            media_uri = "sound:demo-congrats"
            self.logger.info(f"TESTING: Using built-in sound instead of OpenAI audio")

            # Build media URI for Asterisk to fetch (disabled for testing)
            """
            # Use configured host or fallback to auto-detection
            if self.http_config.host and self.http_config.host != "0.0.0.0":
                # Use configured host and external port
                media_uri = f"http://{self.http_config.host}:{self.http_config.external_port}/audio/{filename}"
            else:
                # Auto-detect IP by connecting to Asterisk
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.connect((self.ari.config.host, self.ari.config.port))
                    container_ip = s.getsockname()[0]
                finally:
                    s.close()
                media_uri = f"http://{container_ip}:{self.http_config.external_port}/audio/{filename}"
            """

            url = f"{self.ari.config.base_url}/channels/{self.channel_id}/play"
            payload = {
                "media": media_uri
            }

            self.logger.info(f"Playing audio: {media_uri}")

            async with self.ari.session.post(url, json=payload) as resp:
                response_text = await resp.text()
                self.logger.info(f"ARI playback response: status={resp.status}, body={response_text}")
                if resp.status == 201:
                    playback = json.loads(response_text) if response_text else {}
                    self.logger.info(f"Audio playback started: {playback.get('id')}")
                else:
                    self.logger.error(f"Failed to play audio: {resp.status} - {response_text}")

        except Exception as e:
            self.logger.error(f"Error playing audio file: {e}", exc_info=True)

    async def _delete_file_after_delay(self, filepath: str, delay: int):
        """Delete file after delay."""
        await asyncio.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.unlink(filepath)
        except Exception as e:
            self.logger.error(f"Error deleting file {filepath}: {e}")

    async def _cleanup(self):
        """Clean up resources."""
        self.logger.info("Cleaning up audio bridge")

        # Stop HTTP server
        if self.http_runner:
            await self.http_runner.cleanup()

        # Clean up temp directory
        try:
            import shutil
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
        except Exception as e:
            self.logger.error(f"Error cleaning up temp dir: {e}")

    async def stop(self):
        """Stop the audio bridge."""
        self.logger.info("Stopping audio bridge")
        self.running = False
