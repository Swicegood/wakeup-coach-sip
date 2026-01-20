"""OpenAI Realtime API client for conversational audio."""

import asyncio
import base64
import json
import logging
from typing import Optional, Callable

import websockets
from websockets.client import WebSocketClientProtocol

from config import OpenAIConfig


class OpenAIRealtimeClient:
    """Client for OpenAI Realtime API."""

    REALTIME_API_URL = "wss://api.openai.com/v1/realtime"

    def __init__(
        self,
        config: OpenAIConfig,
        logger: logging.Logger,
        wake_keyword: str = "awake"
    ):
        """
        Initialize OpenAI Realtime client.

        Args:
            config: OpenAI configuration
            logger: Logger instance
            wake_keyword: Keyword to detect for wakefulness
        """
        self.config = config
        self.logger = logger
        self.wake_keyword = wake_keyword.lower()
        self.ws: Optional[WebSocketClientProtocol] = None
        self.is_awake = False
        self.on_audio_data: Optional[Callable] = None
        self.on_wake_detected: Optional[Callable] = None
        self.on_user_speech: Optional[Callable] = None  # Callback for user speech/transcription

        # Audio buffer for RTP streaming
        self.audio_buffer = bytearray()
        self.audio_lock = asyncio.Lock()

        # Rolling stats for audio rate debugging
        self._audio_bytes_since_log = 0
        self._audio_last_log_ts = asyncio.get_event_loop().time()

    async def connect(self):
        """Connect to OpenAI Realtime API WebSocket."""
        url = f"{self.REALTIME_API_URL}?model={self.config.model}"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "OpenAI-Beta": "realtime=v1"
        }

        self.logger.info(f"Connecting to OpenAI Realtime API: {self.config.model}")

        try:
            # Clear audio buffer on reconnect to avoid stale data
            async with self.audio_lock:
                self.audio_buffer.clear()
                self.logger.info("Audio buffer cleared for fresh connection")
            
            # Increase ping timeout to prevent premature disconnection
            self.ws = await websockets.connect(
                url,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=60,
            )
            self.logger.info("Connected to OpenAI Realtime API")
            await self._initialize_session()
        except Exception as e:
            self.logger.error(f"Failed to connect to OpenAI: {e}")
            raise

    async def _initialize_session(self):
        """Initialize the session with instructions."""
        session_config = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": (
                    "You are a friendly wake-up coach. Your job is to have a "
                    "natural conversation with the user to help them wake up. "
                    "Ask them questions, make jokes, discuss their plans for the day. "
                    "Keep the conversation engaging and energetic. "
                    "The call will end when the user says the wake word."
                ),
                "voice": "alloy",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "whisper-1"
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.85,  # High = requires clear speech, reduces false positives
                    "prefix_padding_ms": 400,  # More padding before speech
                    "silence_duration_ms": 1000  # 1 second silence before considering speech ended
                }
            }
        }

        await self.ws.send(json.dumps(session_config))
        self.logger.info("Session initialized")

    async def send_audio(self, audio_data: bytes):
        """
        Send audio data to OpenAI.

        Args:
            audio_data: Raw audio bytes (PCM16)
        """
        if not self.ws:
            raise RuntimeError("WebSocket not connected")

        audio_b64 = base64.b64encode(audio_data).decode('utf-8')

        message = {
            "type": "input_audio_buffer.append",
            "audio": audio_b64
        }

        await self.ws.send(json.dumps(message))

    async def commit_audio(self):
        """Commit the audio buffer for processing."""
        if not self.ws:
            raise RuntimeError("WebSocket not connected")

        message = {"type": "input_audio_buffer.commit"}
        await self.ws.send(json.dumps(message))

    async def listen_for_responses(self):
        """Listen for responses from OpenAI."""
        if not self.ws:
            raise RuntimeError("WebSocket not connected")

        self.logger.info("Listening for OpenAI responses...")

        try:
            async for message in self.ws:
                if not self.ws:  # Check if connection still valid
                    self.logger.warning("WebSocket closed during listen")
                    break
                try:
                    event = json.loads(message)
                    event_type = event.get("type")

                    self.logger.debug(f"OpenAI event: {event_type}")

                    if event_type == "response.audio.delta":
                        await self._handle_audio_delta(event)
                    elif event_type == "conversation.item.input_audio_transcription.completed":
                        # User input transcription
                        self.logger.debug(f"User input transcription event: {event}")
                        await self._handle_transcription(event)
                    elif event_type == "response.audio_transcript.done":
                        # This is the AI's transcription of its own output, not user input
                        # Skip it for user transcription, but log for debugging
                        self.logger.debug(f"AI response transcription (skipping): {event.get('transcript', '')[:100]}")
                    elif event_type == "response.audio_transcript.delta":
                        # Handle incremental transcription updates (optional, for real-time display)
                        pass  # We'll wait for the .done event for final transcript
                    elif event_type == "conversation.item.created":
                        # Check if this is a user message item with transcription
                        item = event.get("item", {})
                        if item.get("type") == "message" and item.get("role") == "user":
                            # Check for transcription in content
                            content = item.get("content", [])
                            for part in content:
                                if part.get("type") == "input_audio_transcription":
                                    transcript = part.get("transcript", "")
                                    if transcript:
                                        self.logger.info(f"User said: {transcript}")
                                        
                                        # Call user speech callback if set
                                        if self.on_user_speech:
                                            await self.on_user_speech(transcript)
                                        
                                        if self.wake_keyword in transcript.lower():
                                            self.logger.info(f"Wake keyword '{self.wake_keyword}' detected!")
                                            self.is_awake = True
                                            if self.on_wake_detected:
                                                await self.on_wake_detected()
                    elif event_type == "input_audio_buffer.speech_started":
                        # VAD detected user started speaking
                        self.logger.info("User speech detected (VAD start)")
                        # Call user speech callback to reset silence timer
                        if self.on_user_speech:
                            await self.on_user_speech("[speech_started]")
                    elif event_type == "input_audio_buffer.speech_stopped":
                        # VAD detected user stopped speaking
                        self.logger.info("User speech ended (VAD stop)")
                    elif event_type == "error":
                        self.logger.error(f"OpenAI error: {event.get('error')}")
                    elif event_type == "session.created":
                        self.logger.info("Session created")
                    elif event_type == "session.updated":
                        self.logger.info("Session updated")

                except json.JSONDecodeError as e:
                    self.logger.error(f"Failed to parse OpenAI event: {e}")
                except Exception as e:
                    self.logger.error(f"Error handling OpenAI event: {e}", exc_info=True)

        except websockets.exceptions.ConnectionClosed:
            self.logger.warning("OpenAI WebSocket connection closed")
        except Exception as e:
            self.logger.error(f"Error in OpenAI listener: {e}", exc_info=True)

    async def _handle_audio_delta(self, event: dict):
        """
        Handle audio delta from OpenAI.

        Args:
            event: Audio delta event
        """
        audio_b64 = event.get("delta")
        if audio_b64:
            audio_bytes = base64.b64decode(audio_b64)

            # Debug: estimate OpenAI output sample rate from bytes/sec (mono PCM16)
            now = asyncio.get_event_loop().time()
            self._audio_bytes_since_log += len(audio_bytes)
            dt = now - self._audio_last_log_ts
            if dt >= 1.0:
                samples = self._audio_bytes_since_log / 2.0
                est_hz = samples / dt
                self.logger.info(f"OpenAI audio rate estimate: ~{est_hz:.0f} Hz (over {dt:.2f}s)")
                self._audio_bytes_since_log = 0
                self._audio_last_log_ts = now

            # Add to buffer for RTP streaming
            async with self.audio_lock:
                old_size = len(self.audio_buffer)
                self.audio_buffer.extend(audio_bytes)
                new_size = len(self.audio_buffer)

                # Log periodically
                if new_size % 10000 < len(audio_bytes) or old_size == 0:
                    self.logger.info(f"OpenAI audio delta: +{len(audio_bytes)} bytes, buffer now {new_size} bytes")

            # Call callback if set
            if self.on_audio_data:
                await self.on_audio_data(audio_bytes)

    async def _handle_transcription(self, event: dict):
        """
        Handle transcription from OpenAI.

        Args:
            event: Transcription event (can be from conversation.item or response.audio_transcript)
        """
        # Try different possible fields for transcript
        # For response.audio_transcript.done, the transcript is in event["transcript"]
        # For conversation.item.input_audio_transcription.completed, it might be in event["transcript"] or nested
        transcript = event.get("transcript") or event.get("text") or ""
        
        # If transcript is a dict, try to extract from it
        if isinstance(transcript, dict):
            transcript = transcript.get("transcript") or transcript.get("text") or ""
        
        # Log the event for debugging if transcript is missing
        if not transcript:
            self.logger.debug(f"Transcription event structure: {list(event.keys())}")
            self.logger.debug(f"Event content (first 500 chars): {str(event)[:500]}")
            return
        
        transcript_lower = transcript.lower().strip()
        if transcript_lower:
            self.logger.info(f"User said: {transcript}")

            # Call user speech callback if set (for sleep detection and goodbye handling)
            if self.on_user_speech:
                await self.on_user_speech(transcript)

            if self.wake_keyword in transcript_lower:
                self.logger.info(f"Wake keyword '{self.wake_keyword}' detected!")
                self.is_awake = True
                if self.on_wake_detected:
                    await self.on_wake_detected()

    async def close(self):
        """Close the WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.ws = None
            self.logger.info("OpenAI connection closed")
        
        # Clear audio buffer on close
        async with self.audio_lock:
            self.audio_buffer.clear()