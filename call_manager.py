"""Call lifecycle manager for wake-up coach."""

import asyncio
import json
import logging
import os
from typing import Optional, Callable
from datetime import datetime, time as dt_time, timedelta
from enum import Enum

from ari_client import ARIClient
from openai_client import OpenAIRealtimeClient
from audio_bridge_rtp import AudioBridgeRTP
from config import Config
from doorbell_webhook import DoorbellWebhook


class CallState(Enum):
    """Call state machine states."""
    CALL_ACTIVE = "call_active"
    WAITING_FOR_USER_AFTER_SLEEP_PROMPT = "waiting_for_user_after_sleep_prompt"
    CALL_ENDED = "call_ended"


class CallManager:
    """Manages the lifecycle of wake-up calls."""

    def __init__(self, config: Config, logger: logging.Logger):
        """
        Initialize call manager.

        Args:
            config: Application configuration
            logger: Logger instance
        """
        self.config = config
        self.logger = logger
        self.ari: Optional[ARIClient] = None
        self.openai: Optional[OpenAIRealtimeClient] = None
        self.bridge: Optional[AudioBridgeRTP] = None
        self.doorbell: Optional[DoorbellWebhook] = None
        self.current_channel_id: Optional[str] = None
        self.call_active = False
        
        # State machine
        self.state = CallState.CALL_ENDED
        
        # Sleep detection
        self.last_user_response_time: Optional[datetime] = None
        self.sleep_check_interval = 10  # seconds - check for sleep after 10s of silence
        self.sleep_check_task: Optional[asyncio.Task] = None
        self.sleep_prompt_response_wait = 10  # seconds to wait after "Are you sleeping?"
        self.sleep_prompt_task: Optional[asyncio.Task] = None
        self.user_response_detected = False  # Flag set when user responds during sleep prompt
        
        # Call-back loop
        self.call_backoff_seconds = 2  # Wait 2 seconds before calling back
        self.should_call_back = True  # Flag to control call-back loop
        self.sleep_detected = False  # Only call back if sleep was detected
        self.conversation_starting = False  # Flag to prevent race conditions during conversation setup
        self.call_attempt = 0  # increments each originate; callbacks are attempts > 1

    async def start(self):
        """Start the call manager and initiate a wake-up call."""
        self.logger.info("Starting call manager")

        # Initialize doorbell webhook
        doorbell_timeout_minutes = int(os.getenv("DOORBELL_TIMEOUT_MINUTES", "5"))
        self.doorbell = DoorbellWebhook(self.logger, timeout_minutes=doorbell_timeout_minutes)

        # Initialize clients
        self.ari = ARIClient(self.config.asterisk, self.logger)
        self.openai = OpenAIRealtimeClient(
            self.config.openai,
            self.logger,
            wake_keyword=self.config.call.wake_keyword
        )

        # Set up callbacks
        self.openai.on_user_speech = self._handle_user_speech  # Track user responses
        self.openai.on_wake_detected = self._handle_wake_detected

        async with self.ari:
            # Connect to ARI WebSocket
            await self.ari.connect_websocket()

            # Register event handlers
            self.ari.on_event("StasisStart", self._handle_stasis_start)
            self.ari.on_event("ChannelStateChange", self._handle_channel_state_change)
            self.ari.on_event("ChannelHangupRequest", self._handle_hangup_request)
            self.ari.on_event("ChannelDestroyed", self._handle_channel_destroyed)

            # Connect to OpenAI
            await self.openai.connect()

            # Start doorbell webhook server
            await self._start_doorbell_webhook_server()

            # Wait for scheduled wake-up time (if configured)
            await self._wait_for_wake_time()

            # Start event listener and call-back loop in parallel
            self.logger.info("Starting event listener and call-back loop...")
            results = await asyncio.gather(
                self.ari.listen_for_events(),
                self._call_back_loop(),
                return_exceptions=True
            )
            
            # Log any exceptions that occurred
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    task_name = ["event listener", "call-back loop"][i]
                    self.logger.error(f"Error in {task_name}: {result}", exc_info=result)

    async def _start_doorbell_webhook_server(self):
        """Start HTTP server for doorbell webhook."""
        from aiohttp import web
        
        app = self.doorbell.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        
        webhook_port = int(os.getenv("DOORBELL_WEBHOOK_PORT", "8080"))
        site = web.TCPSite(runner, "0.0.0.0", webhook_port)
        await site.start()
        
        self.logger.info(f"Doorbell webhook server started on port {webhook_port}")

    async def _call_back_loop(self):
        """Main loop that calls back when sleep is detected."""
        while self.should_call_back:
            # Reset sleep detection flag for this call
            self.sleep_detected = False
            
            # Originate the call
            self.call_attempt += 1
            await self._originate_call()
            
            if not self.call_active:
                # Call failed to originate, wait before retrying
                self.logger.info(f"Call not active, waiting {self.call_backoff_seconds}s before retry...")
                await asyncio.sleep(self.call_backoff_seconds)
                continue
            
            # Wait for call to end (either normally or due to sleep detection)
            # The call will end via _handle_channel_destroyed or _end_call_for_sleep
            # Also add a timeout in case channel never enters Stasis
            call_start_time = datetime.now()
            call_timeout_seconds = 60  # If no StasisStart after 60s, assume call failed
            
            while self.call_active:
                await asyncio.sleep(1)
                
                # Check if call has timed out (never entered Stasis)
                if self.current_channel_id and self.state == CallState.CALL_ENDED:
                    # Channel was destroyed before entering Stasis
                    elapsed = (datetime.now() - call_start_time).total_seconds()
                    if elapsed > call_timeout_seconds:
                        self.logger.warning(f"Call {self.current_channel_id} never entered Stasis after {elapsed:.1f}s, assuming failed")
                        self.call_active = False
                        break
                
                # Also check if channel still exists (if we can query it)
                # For now, rely on events
            
            # Call back if:
            # - Sleep was explicitly detected (via silence + no response to prompt)
            # - User hung up (they might have fallen asleep)
            # Don't call back if:
            # - User properly ended call (doorbell + goodbye) - should_call_back will be False
            if self.should_call_back:
                if self.sleep_detected:
                    self.logger.info(f"Sleep detected - calling back in {self.call_backoff_seconds}s...")
                else:
                    self.logger.info(f"Call ended (user may have fallen asleep) - calling back in {self.call_backoff_seconds}s...")
                await asyncio.sleep(self.call_backoff_seconds)

    async def _wait_for_wake_time(self):
        """Wait until the scheduled wake-up time before calling."""
        if not self.config.call.wake_up_time:
            self.logger.info("No wake-up time configured, calling immediately")
            return

        try:
            # Parse wake-up time (HH:MM format)
            wake_hour, wake_minute = map(int, self.config.call.wake_up_time.split(":"))
            wake_time = dt_time(wake_hour, wake_minute)
        except (ValueError, AttributeError) as e:
            self.logger.warning(f"Invalid wake-up time format '{self.config.call.wake_up_time}', calling immediately: {e}")
            return

        now = datetime.now()
        wake_datetime = datetime.combine(now.date(), wake_time)

        # If wake time has already passed today, schedule for tomorrow
        if wake_datetime <= now:
            wake_datetime += timedelta(days=1)

        wait_seconds = (wake_datetime - now).total_seconds()
        wait_hours = wait_seconds / 3600

        self.logger.info(f"Scheduled wake-up call for {wake_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"Waiting {wait_hours:.2f} hours ({wait_seconds:.0f} seconds) until wake-up time...")

        # Wait until wake-up time
        await asyncio.sleep(wait_seconds)

        self.logger.info(f"Wake-up time reached! Originating call...")

    async def _originate_call(self):
        """Originate a wake-up call with retry logic."""
        # Use Local channel through wakeup-trigger context which:
        # 1. Dials out via PJSIP trunk
        # 2. Uses G() option to enter Stasis on the PJSIP leg after answer
        # The dialplan calls Stasis(wakeup-coach) on the answered PJSIP channel
        number = self.config.call.target_phone_number
        
        # Remove leading + for dialplan routing
        dial_number = number.lstrip("+")
        # Use wakeup-trigger context which Dial()s out, then uses G() to enter Stasis on the PJSIP leg
        endpoint = f"Local/{dial_number}@wakeup-trigger"
        context = "wakeup-trigger"

        self.logger.info(f"Originating call: endpoint={endpoint}, extension={dial_number}@{context}")

        max_retries = 3
        retry_delay = 5  # seconds

        for attempt in range(1, max_retries + 1):
            try:
                channel = await self.ari.originate_call(
                    endpoint=endpoint,
                    caller_id="Wake Up Coach",
                    extension=dial_number,
                    context=context
                )
                self.current_channel_id = channel.get("id")
                self.call_active = True
                self.state = CallState.CALL_ACTIVE
                # Don't set last_user_response_time here - wait for actual user speech
                self.logger.info(f"Call originated successfully: {self.current_channel_id}")
                return

            except Exception as e:
                self.logger.error(f"Failed to originate call (attempt {attempt}/{max_retries}): {e}")
                
                if attempt < max_retries:
                    self.logger.info(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                else:
                    self.logger.error("Failed to originate call after all retries.")
                    self.call_active = False

    async def _handle_stasis_start(self, event: dict):
        """Handle StasisStart event when call enters the application."""
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_name = channel.get("name", "")
        channel_state = channel.get("state")
        
        self.logger.info(f"StasisStart: channel={channel_id}, name={channel_name}, state={channel_state}")
        
        # Handle PJSIP channel (from dialplan Stasis entry)
        if channel_name.startswith("PJSIP/"):
            self.logger.info(f"PJSIP channel {channel_id} entered Stasis - this is our outbound call!")
            self.current_channel_id = channel_id
            self.state = CallState.CALL_ACTIVE
            self.call_active = True
            # Don't set last_user_response_time here - wait for actual user speech
            
            if channel_state != "Up":
                await self.ari.answer_channel(channel_id)
            
            # Start conversation if not already started
            if not self.bridge:
                await self._start_conversation(channel_id)
        
        # Handle UnicastRTP channel (external media for audio bridge)
        elif channel_name.startswith("UnicastRTP/"):
            self.logger.info(f"External media channel {channel_id} entered Stasis")
            # The audio bridge was created - use the phone channel we're tracking
            if self.current_channel_id and not self.bridge:
                self.logger.info(f"Starting conversation via external media channel")
                await self._start_conversation(self.current_channel_id)

    async def _handle_channel_state_change(self, event: dict):
        """Handle channel state changes."""
        channel_id = event.get("channel", {}).get("id")
        state = event.get("channel", {}).get("state")
        channel_name = event.get("channel", {}).get("name", "")

        self.logger.info(f"Channel {channel_id} ({channel_name}) state changed to: {state}")
        
        # When originating a call, start conversation when it's answered
        # This handles the case where the channel doesn't explicitly enter Stasis
        if state == "Up" and channel_id == self.current_channel_id and not self.bridge:
            self.logger.info(f"Call answered, starting conversation")
            self.state = CallState.CALL_ACTIVE
            # Don't set last_user_response_time here - wait for actual user speech
            await self._start_conversation(channel_id)

    async def _start_conversation(self, channel_id: str):
        """Start the conversation with OpenAI."""
        # Prevent race conditions - only one conversation setup at a time
        if self.conversation_starting:
            self.logger.warning(f"Conversation already starting, ignoring for {channel_id}")
            return
        
        self.conversation_starting = True
        self.logger.info(f"Starting conversation on channel {channel_id}")

        try:
            # ALWAYS create fresh OpenAI session for each call
            # Close existing connection if any (might be stale from previous call)
            if self.openai.ws:
                self.logger.info("Closing existing OpenAI connection for fresh session...")
                await self.openai.close()
            
            self.logger.info("Connecting to OpenAI for new call...")
            await self.openai.connect()
            
            # Create audio bridge using RTP
            self.bridge = AudioBridgeRTP(self.ari, self.openai, self.logger)

            # Start sleep detection
            self.sleep_check_task = asyncio.create_task(self._sleep_detection_loop())

            # Start bridging audio (this will run until call ends or wake detected)
            await self.bridge.start(channel_id, prime_rtp=(self.call_attempt > 1))

        except Exception as e:
            self.logger.error(f"Error in conversation: {e}", exc_info=True)
            # Just cleanup - don't stop callback loop on errors
            await self._cleanup()
            self.call_active = False
            self.state = CallState.CALL_ENDED
        finally:
            self.conversation_starting = False

    async def _sleep_detection_loop(self):
        """Continuously check if user has stopped responding (10 seconds of silence)."""
        self.logger.info(f"Sleep detection loop started. Interval: {self.sleep_check_interval}s")
        loop_count = 0
        
        # Wait for greeting to complete before starting silence detection
        # This gives time for: test tone (1s) + OpenAI greeting (~5-10s) + buffer
        initial_grace_period = 15  # seconds
        self.logger.info(f"Sleep detection: waiting {initial_grace_period}s for greeting to complete...")
        
        # Wait for grace period OR until user speaks (whichever comes first)
        grace_start = datetime.now()
        while self.call_active and self.state != CallState.CALL_ENDED:
            await asyncio.sleep(1)
            elapsed = (datetime.now() - grace_start).total_seconds()
            
            # If user has spoken, we can start the silence timer
            if self.last_user_response_time:
                self.logger.info(f"User spoke during grace period - starting silence detection")
                break
            
            # Grace period complete
            if elapsed >= initial_grace_period:
                self.logger.info(f"Grace period complete ({initial_grace_period}s) - starting silence detection")
                # Set last_user_response_time to now so the 10s timer starts
                self.last_user_response_time = datetime.now()
                break
        
        # Main silence detection loop
        while self.call_active and self.state != CallState.CALL_ENDED:
            await asyncio.sleep(1)
            loop_count += 1
            
            if not self.call_active:
                self.logger.info("Sleep detection loop: call no longer active, exiting")
                break
            
            # Check for silence (only when in CALL_ACTIVE state)
            if self.last_user_response_time and self.state == CallState.CALL_ACTIVE:
                time_since_response = (datetime.now() - self.last_user_response_time).total_seconds()
                
                # Log every 5 seconds
                if loop_count % 5 == 0:
                    self.logger.debug(f"Sleep detection: {time_since_response:.1f}s since last response (threshold: {self.sleep_check_interval}s)")
                
                # If no response for sleep_check_interval (10 seconds), prompt user
                if time_since_response >= self.sleep_check_interval:
                    self.logger.info(f"No user response for {time_since_response:.1f}s, prompting for sleep check...")
                    await self._prompt_sleep_check()

    async def _prompt_sleep_check(self):
        """Prompt user with 'Are you sleeping?' and wait for response."""
        if self.state != CallState.CALL_ACTIVE:
            return
        
        self.logger.info("Prompting user: 'Are you sleeping?'")
        self.state = CallState.WAITING_FOR_USER_AFTER_SLEEP_PROMPT
        self.user_response_detected = False
        
        # Send prompt to OpenAI and trigger it to speak
        if self.openai and self.openai.ws:
            # Create the message
            prompt_message = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Ask me if I'm sleeping - just say 'Are you sleeping?' and nothing else."}]
                }
            }
            await self.openai.ws.send(json.dumps(prompt_message))
            
            # Trigger OpenAI to respond (speak the message)
            response_create = {
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "instructions": "Ask if the user is sleeping. Just say 'Are you sleeping?' - keep it very brief."
                }
            }
            await self.openai.ws.send(json.dumps(response_create))
            self.logger.info("Sleep check prompt sent to OpenAI")
        
        # Wait for user response
        self.sleep_prompt_task = asyncio.create_task(self._wait_for_sleep_prompt_response())

    async def _wait_for_sleep_prompt_response(self):
        """Wait for user response after sleep prompt."""
        await asyncio.sleep(self.sleep_prompt_response_wait)
        
        if not self.user_response_detected and self.call_active:
            # No response - user is sleeping, hang up and call back
            self.logger.info("No response to sleep prompt - user appears to be sleeping. Hanging up to call back...")
            await self._end_call_for_sleep()
        elif self.user_response_detected:
            # User responded, return to normal conversation
            self.logger.info("User responded to sleep prompt, continuing conversation")
            self.state = CallState.CALL_ACTIVE
            self.last_user_response_time = datetime.now()

    async def _handle_user_speech(self, transcript: str):
        """Handle user speech/transcription."""
        is_vad_event = transcript.startswith("[")
        
        # VAD events (speech_started) only count during sleep prompt waiting
        # This prevents background noise from resetting the main silence timer
        if is_vad_event:
            self.logger.debug(f"VAD event: {transcript}")
            # Only reset if waiting for sleep prompt response
            if self.state == CallState.WAITING_FOR_USER_AFTER_SLEEP_PROMPT:
                self.user_response_detected = True
                self.logger.info("VAD detected during sleep prompt - user responded")
                if self.sleep_prompt_task:
                    self.sleep_prompt_task.cancel()
            return
        
        # Real transcription - reset silence timer and log
        self.last_user_response_time = datetime.now()
        self.logger.info(f"User said: {transcript[:100] if transcript else 'N/A'}")
        self.logger.info(f"Reset last_user_response_time to {self.last_user_response_time}")
        
        # Check if we're waiting for response to sleep prompt
        if self.state == CallState.WAITING_FOR_USER_AFTER_SLEEP_PROMPT:
            self.user_response_detected = True
            self.logger.info("User responded to sleep prompt, resuming normal conversation")
            self.state = CallState.CALL_ACTIVE  # Resume normal state immediately
            if self.sleep_prompt_task:
                self.sleep_prompt_task.cancel()
        
        # Check for goodbye/end call with doorbell requirement
        transcript_lower = transcript.lower().strip()
        if any(word in transcript_lower for word in ["goodbye", "end call", "hang up"]):
            if self.doorbell and self.doorbell.is_activated():
                self.logger.info("User said goodbye and doorbell is activated - ending call permanently")
                self.should_call_back = False
                await self._end_call()
            else:
                self.logger.info("User said goodbye but doorbell not activated - refusing to end call")
                # Send response via OpenAI
                if self.openai and self.openai.ws:
                    response_message = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "To end this wake-up call, please touch the doorbell first."}]
                        }
                    }
                    await self.openai.ws.send(json.dumps(response_message))

    async def _handle_wake_detected(self):
        """Handle wake keyword detection (legacy - now handled by _handle_user_speech)."""
        # This is kept for backward compatibility but wake detection is now
        # handled through the goodbye/end call logic in _handle_user_speech
        self.logger.info("Wake keyword detected (legacy handler)")

    async def _handle_hangup_request(self, event: dict):
        """Handle hangup request."""
        channel_id = event.get("channel", {}).get("id")
        self.logger.info(f"Hangup requested for channel {channel_id}")

    async def _handle_channel_destroyed(self, event: dict):
        """Handle channel destruction."""
        channel_id = event.get("channel", {}).get("id")
        channel_name = event.get("channel", {}).get("name", "")
        cause_txt = event.get("cause_txt", "Unknown")
        cause = event.get("cause", 0)

        self.logger.info(f"Channel {channel_id} ({channel_name}) destroyed: {cause_txt} (cause: {cause})")

        # Ignore Local channel destruction - we only care about PJSIP channels
        # Local channels are used for routing and get destroyed early in call setup
        if channel_name.startswith("Local/"):
            self.logger.debug(f"Ignoring Local channel destruction: {channel_name}")
            return

        if channel_id == self.current_channel_id:
            self.call_active = False
            self.state = CallState.CALL_ENDED
            
            # Log different causes for debugging
            if cause == 16:  # Normal clearing
                self.logger.info("Call ended normally")
            elif cause == 21:  # User busy
                self.logger.warning("User was busy - call not answered")
            elif cause == 27:  # Destination out of order
                self.logger.error("Destination out of order - check phone number")
            elif cause == 34:  # Circuit/channel congestion
                self.logger.error("Circuit congestion - trunk may be unavailable")
            else:
                self.logger.warning(f"Call ended with cause {cause}: {cause_txt}")
            
            await self._cleanup()

    async def _end_call(self):
        """End the current call."""
        if self.current_channel_id and self.call_active:
            self.logger.info("Ending call")
            self.should_call_back = False  # Don't call back if ending normally

            try:
                await self.ari.hangup_channel(self.current_channel_id)
            except Exception as e:
                self.logger.error(f"Error hanging up channel: {e}")

            self.call_active = False
            self.state = CallState.CALL_ENDED

    async def _end_call_for_sleep(self):
        """End call because user appears to be sleeping (will call back)."""
        if self.current_channel_id and self.call_active:
            self.logger.info("Ending call due to sleep detection (will call back)")
            self.sleep_detected = True  # Signal to call-back loop to call again

            try:
                await self.ari.hangup_channel(self.current_channel_id)
            except Exception as e:
                self.logger.error(f"Error hanging up channel: {e}")

            # Clean up resources (audio bridge, OpenAI) before callback
            await self._cleanup()
            
            self.call_active = False
            self.state = CallState.CALL_ENDED

    async def _cleanup(self):
        """Clean up resources gracefully."""
        self.logger.info("Cleaning up resources")

        # Cancel sleep detection tasks (but not if we're being called from one of them!)
        # Get current task to avoid cancelling ourselves
        current_task = asyncio.current_task()
        
        if self.sleep_check_task and self.sleep_check_task != current_task:
            self.sleep_check_task.cancel()
        if self.sleep_prompt_task and self.sleep_prompt_task != current_task:
            self.sleep_prompt_task.cancel()

        try:
            if self.bridge:
                await self.bridge.stop()
        except Exception as e:
            self.logger.error(f"Error stopping audio bridge: {e}", exc_info=True)

        try:
            if self.openai:
                await self.openai.close()
        except Exception as e:
            self.logger.error(f"Error closing OpenAI connection: {e}", exc_info=True)

        self.current_channel_id = None
        self.bridge = None
        self.last_user_response_time = None
