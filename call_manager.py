"""Call lifecycle manager for wake-up coach."""

import asyncio
import json
import logging
import os
from typing import Optional, Callable
from datetime import datetime, timedelta, time as dt_time, timedelta
from enum import Enum

# Try to use zoneinfo (Python 3.9+), fallback to pytz
try:
    from zoneinfo import ZoneInfo
    HAS_ZONEINFO = True
except ImportError:
    try:
        import pytz
        HAS_ZONEINFO = False
    except ImportError:
        raise ImportError("Either zoneinfo (Python 3.9+) or pytz is required for timezone support")

from ari_client import ARIClient
from openai_client import OpenAIRealtimeClient
from coach_prompts import (
    GOODBYE_RESPONSE_INSTRUCTIONS,
    SLEEP_CHECK_RESPONSE_INSTRUCTIONS,
    SLEEP_CHECK_USER_PROMPT,
)
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
        self.sleep_prompt_response_wait = 10  # seconds to wait after the sleep-check prompt
        self.sleep_prompt_task: Optional[asyncio.Task] = None
        self.user_response_detected = False  # Flag set when user responds during sleep prompt
        
        # Call-back loop
        self.call_backoff_seconds = 2  # Wait 2 seconds before calling back
        self.should_call_back = True  # Flag to control call-back loop
        self.sleep_detected = False  # Only call back if sleep was detected
        self.conversation_starting = False  # Flag to prevent race conditions during conversation setup
        self.cleaning_up = False  # Flag to prevent multiple cleanups
        self.originating_call = False  # Flag to prevent concurrent call origination
        self.call_attempt = 0  # increments each originate; callbacks are attempts > 1
        self.cleanup_complete = asyncio.Event()  # Event to signal cleanup is complete
        self.cleanup_complete.set()  # Initially set (no cleanup needed)
        self.goodbye_response_complete = asyncio.Event()  # Event to signal goodbye message is done
        self.waiting_for_goodbye_response = False  # Flag to track if we're waiting for goodbye

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
        self.openai.on_ai_speech_finished = self._handle_ai_speech_finished  # Track when AI finishes speaking

        async with self.ari:
            # Connect to ARI WebSocket
            await self.ari.connect_websocket()

            # Register event handlers
            self.ari.on_event("StasisStart", self._handle_stasis_start)
            self.ari.on_event("StasisEnd", self._handle_stasis_end)
            self.ari.on_event("ChannelStateChange", self._handle_channel_state_change)
            self.ari.on_event("ChannelHangupRequest", self._handle_hangup_request)
            self.ari.on_event("ChannelDestroyed", self._handle_channel_destroyed)
            self.logger.info(f"Registered event handlers: {list(self.ari.event_handlers.keys())}")

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
            
            # Wait for cleanup to complete before calling back
            # This prevents race conditions where a new call starts before the old one is fully cleaned up
            # Clear the event first (in case cleanup hasn't started yet) then wait for it
            if not self.cleaning_up:
                # Cleanup hasn't started yet - clear the event so we wait for it
                self.cleanup_complete.clear()
                self.logger.info("Call ended, waiting for cleanup to start and complete...")
            else:
                self.logger.info("Cleanup already in progress, waiting for it to complete...")
            
            try:
                await asyncio.wait_for(self.cleanup_complete.wait(), timeout=5.0)
                self.logger.info("Cleanup complete, proceeding with callback")
            except asyncio.TimeoutError:
                self.logger.warning("Cleanup timeout - proceeding anyway (may have timing issues)")
                # Set the event so we don't block next time
                self.cleanup_complete.set()
            
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
        # If CALL_IMMEDIATELY is set, skip waiting
        if self.config.call.call_immediately:
            self.logger.info("CALL_IMMEDIATELY=true - calling immediately (testing mode)")
            return
        
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

        # Get timezone-aware current time
        try:
            if HAS_ZONEINFO:
                tz = ZoneInfo(self.config.call.timezone)
                utc_tz = ZoneInfo("UTC")
            else:
                import pytz
                tz = pytz.timezone(self.config.call.timezone)
                utc_tz = pytz.UTC
            self.logger.info(f"Using timezone: {self.config.call.timezone}")
        except Exception as e:
            self.logger.warning(f"Invalid timezone '{self.config.call.timezone}', using UTC: {e}")
            if HAS_ZONEINFO:
                tz = ZoneInfo("UTC")
                utc_tz = ZoneInfo("UTC")
            else:
                import pytz
                tz = pytz.UTC
                utc_tz = pytz.UTC

        now = datetime.now(tz)
        # Create timezone-aware wake datetime
        if HAS_ZONEINFO:
            wake_datetime = datetime.combine(now.date(), wake_time, tz)
        else:
            import pytz
            # With pytz, combine creates naive datetime, then localize it
            naive_wake = datetime.combine(now.date(), wake_time)
            wake_datetime = tz.localize(naive_wake)

        # If wake time has already passed today, schedule for tomorrow
        if wake_datetime <= now:
            wake_datetime += timedelta(days=1)

        wait_seconds = (wake_datetime - now).total_seconds()
        wait_hours = wait_seconds / 3600

        # Log in both local timezone and UTC for clarity
        now_utc = now.astimezone(utc_tz)
        wake_utc = wake_datetime.astimezone(utc_tz)
        self.logger.info(f"Current time ({self.config.call.timezone}): {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        self.logger.info(f"Current time (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        self.logger.info(f"Scheduled wake-up call ({self.config.call.timezone}): {wake_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        self.logger.info(f"Scheduled wake-up call (UTC): {wake_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        self.logger.info(f"Waiting {wait_hours:.2f} hours ({wait_seconds:.0f} seconds) until wake-up time...")

        # Wait until wake-up time
        await asyncio.sleep(wait_seconds)

        self.logger.info(f"Wake-up time reached! Originating call...")

    async def _originate_call(self):
        """Originate a wake-up call with retry logic."""
        # Prevent duplicate calls - if a call is already active or being originated, skip
        if self.originating_call or self.call_active or self.current_channel_id:
            self.logger.warning(f"Skipping duplicate call origination - originating: {self.originating_call}, active: {self.call_active}, channel: {self.current_channel_id}")
            return
        
        self.originating_call = True
        try:
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
                    self.cleanup_complete.set()  # Ensure cleanup_complete is set for new call
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
        finally:
            self.originating_call = False

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
            
            # Answer if needed, then start conversation
            if channel_state != "Up":
                await self.ari.answer_channel(channel_id)
            
            # Start conversation for PJSIP channels if not already started
            if not self.bridge:
                await self._start_conversation(channel_id)
        
        # Handle UnicastRTP channel (external media for audio bridge)
        elif channel_name.startswith("UnicastRTP/"):
            self.logger.info(f"External media channel {channel_id} entered Stasis")
            # The audio bridge was created - use the phone channel we're tracking
            if self.current_channel_id and not self.bridge:
                self.logger.info(f"Starting conversation via external media channel")
                await self._start_conversation(self.current_channel_id)

    async def _handle_stasis_end(self, event: dict):
        """Handle StasisEnd event when a channel leaves the Stasis application."""
        self.logger.info(f"StasisEnd handler called! Event: {event}")
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_name = channel.get("name", "")

        self.logger.info(f"StasisEnd: channel={channel_id}, name={channel_name}, current_channel_id={self.current_channel_id}, call_active={self.call_active}, bridge={self.bridge is not None}")

        # Handle PJSIP channel leaving Stasis - this means the call ended
        # Check by channel_id match OR if it's a PJSIP channel and we still have a bridge (cleanup might have cleared current_channel_id)
        if channel_name.startswith("PJSIP/") and (channel_id == self.current_channel_id or (self.bridge is not None and self.call_active)):
            self.logger.info(f"PJSIP channel {channel_id} left Stasis - call ended (user hung up)")
            self.call_active = False
            self.state = CallState.CALL_ENDED
            self.cleanup_complete.clear()  # Clear event so callback loop waits for cleanup
            
            # IMMEDIATELY close OpenAI to stop it from sending audio
            if self.openai and self.openai.ws:
                self.logger.info("Closing OpenAI connection IMMEDIATELY to stop audio...")
                try:
                    await self.openai.close()
                    self.logger.info("OpenAI connection closed")
                except Exception as e:
                    self.logger.error(f"Error closing OpenAI: {e}")
            
            # IMMEDIATELY stop audio bridge
            if self.bridge:
                self.logger.info("Stopping audio bridge IMMEDIATELY...")
                try:
                    await self.bridge.stop()
                    self.logger.info("Audio bridge stopped")
                except Exception as e:
                    self.logger.error(f"Error stopping audio bridge: {e}")
                self.bridge = None
            
            await self._cleanup()

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
        
        # Wait for initial greeting to complete before starting silence detection
        # The silence timer will be set when OpenAI finishes speaking (via _handle_ai_speech_finished)
        # This gives time for: test tone (1s) + OpenAI greeting (~5-10s) + buffer
        initial_grace_period = 20  # seconds - max wait for initial greeting
        self.logger.info(f"Sleep detection: waiting for OpenAI to finish initial greeting (max {initial_grace_period}s)...")
        
        # Wait for OpenAI to finish speaking (last_user_response_time will be set by _handle_ai_speech_finished)
        # OR timeout after grace period
        grace_start = datetime.now()
        while self.call_active and self.state != CallState.CALL_ENDED:
            # Check call_active more frequently to respond to hangups faster
            for _ in range(5):  # 5 * 0.2s = 1s total
                if not self.call_active or self.state == CallState.CALL_ENDED:
                    self.logger.info("Sleep detection loop: call ended during grace period, exiting")
                    return
                try:
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    self.logger.info("Sleep detection loop cancelled during grace period")
                    raise
            
            elapsed = (datetime.now() - grace_start).total_seconds()
            
            # If OpenAI has finished speaking (last_user_response_time is set), we can start the silence timer
            if self.last_user_response_time:
                self.logger.info(f"OpenAI finished speaking - starting silence detection (timer set to {self.last_user_response_time})")
                break
            
            # Grace period timeout - set timer anyway (fallback if OpenAI speech finished callback didn't fire)
            if elapsed >= initial_grace_period:
                self.logger.warning(f"Grace period complete ({initial_grace_period}s) but OpenAI speech finished callback didn't fire - starting silence detection anyway")
                self.last_user_response_time = datetime.now()
                break
        
        # Main silence detection loop
        try:
            while self.call_active and self.state != CallState.CALL_ENDED:
                # Check call_active more frequently (every 0.2s instead of 1s) to respond to hangups faster
                for _ in range(5):  # 5 * 0.2s = 1s total
                    if not self.call_active or self.state == CallState.CALL_ENDED:
                        self.logger.info("Sleep detection loop: call no longer active, exiting immediately")
                        return  # Exit immediately, don't continue
                    try:
                        await asyncio.sleep(0.2)
                    except asyncio.CancelledError:
                        self.logger.info("Sleep detection loop cancelled")
                        raise
                
                loop_count += 1
                
                # Check for silence (only when in CALL_ACTIVE state)
                if self.last_user_response_time and self.state == CallState.CALL_ACTIVE:
                    time_since_response = (datetime.now() - self.last_user_response_time).total_seconds()
                    
                    # Add 3-second buffer to give user time to process and respond after AI speaks
                    buffer_seconds = 3.0
                    effective_threshold = self.sleep_check_interval + buffer_seconds  # 10s + 3s = 13s total
                    
                    # Log every 5 seconds
                    if loop_count % 5 == 0:
                        self.logger.debug(f"Sleep detection: {time_since_response:.1f}s since AI finished speaking (threshold: {effective_threshold:.1f}s)")
                    
                    # If no response for effective_threshold (13 seconds total), prompt user
                    if time_since_response >= effective_threshold:
                        self.logger.info(f"No user response for {time_since_response:.1f}s after AI finished speaking (threshold: {effective_threshold:.1f}s), prompting for sleep check...")
                        await self._prompt_sleep_check()
        except asyncio.CancelledError:
            self.logger.info("Sleep detection loop cancelled")
            raise

    async def _prompt_sleep_check(self):
        """Prompt user with a quick, gentle check-in and wait for response."""
        if self.state != CallState.CALL_ACTIVE:
            return
        # Avoid duplicate sleep check (e.g. race with user response setting state back)
        if self.sleep_prompt_task and not self.sleep_prompt_task.done():
            self.logger.debug("Sleep prompt already in progress, skipping")
            return

        self.logger.info("Prompting user with quick gentle check-in...")
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
                    "content": [{"type": "input_text", "text": SLEEP_CHECK_USER_PROMPT}]
                }
            }
            await self.openai.ws.send(json.dumps(prompt_message))
            
            # Trigger OpenAI to respond (speak the message)
            response_create = {
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "instructions": SLEEP_CHECK_RESPONSE_INSTRUCTIONS,
                }
            }
            await self.openai.ws.send(json.dumps(response_create))
            self.logger.info("Sleep check prompt sent to OpenAI")
        
        # Wait for user response
        self.sleep_prompt_task = asyncio.create_task(self._wait_for_sleep_prompt_response())

    async def _wait_for_sleep_prompt_response(self):
        """Wait for user response after sleep prompt."""
        # Check call_active frequently during wait to respond to hangups immediately
        wait_iterations = int(self.sleep_prompt_response_wait / 0.2)  # Check every 0.2s
        for _ in range(wait_iterations):
            if not self.call_active or self.state == CallState.CALL_ENDED:
                self.logger.info("Call ended while waiting for sleep prompt response, exiting")
                return
            if self.user_response_detected:
                # User responded, return to normal conversation
                self.logger.info("User responded to sleep prompt, continuing conversation")
                self.state = CallState.CALL_ACTIVE
                self.last_user_response_time = datetime.now()
                return
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                self.logger.info("Sleep prompt response wait cancelled")
                raise
        
        # Wait complete - check if user responded
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
                self.state = CallState.CALL_ACTIVE
                self.last_user_response_time = datetime.now()  # reset silence timer so we don't re-prompt in 1s
                if self.sleep_prompt_task:
                    self.sleep_prompt_task.cancel()
            return
        
        # Real transcription - reset silence timer so a short pause (e.g. 3s) doesn't trigger sleep check
        self.logger.info(f"User said: {transcript[:100] if transcript else 'N/A'}")
        if self.openai and self.openai.ws:
            try:
                await self.openai.update_coach_state_from_user_text(transcript)
            except Exception as e:
                self.logger.warning(f"Coach state update failed: {e}")
        if self.state == CallState.CALL_ACTIVE:
            self.last_user_response_time = datetime.now()
            self.logger.debug("User spoke - reset silence timer (13s countdown restarts)")
        
        # Check if we're waiting for response to sleep prompt
        if self.state == CallState.WAITING_FOR_USER_AFTER_SLEEP_PROMPT:
            self.user_response_detected = True
            self.logger.info("User responded to sleep prompt, resuming normal conversation")
            self.state = CallState.CALL_ACTIVE  # Resume normal state immediately
            self.last_user_response_time = datetime.now()  # reset silence timer so sleep loop doesn't re-prompt in 1s
            if self.sleep_prompt_task:
                self.sleep_prompt_task.cancel()
        
        # Check for goodbye/end call with doorbell requirement
        transcript_lower = transcript.lower().strip()
        if any(word in transcript_lower for word in ["goodbye", "end call", "hang up"]):
            doorbell_activated = False
            if self.doorbell:
                doorbell_activated = self.doorbell.is_activated()
                activation_time = self.doorbell.get_activation_time()
                self.logger.info(f"Goodbye detected - doorbell exists: {self.doorbell is not None}, activated: {doorbell_activated}, activation_time: {activation_time}")
            else:
                self.logger.warning("Goodbye detected but doorbell webhook handler is None!")
            
            if doorbell_activated:
                self.logger.info("User said goodbye and doorbell is activated - sending goodbye message then ending call")
                self.should_call_back = False
                
                # Send an encouraging goodbye message via OpenAI and trigger it to speak
                if self.openai and self.openai.ws:
                    # Reset and prepare to wait for goodbye response
                    self.goodbye_response_complete.clear()
                    self.waiting_for_goodbye_response = True
                    
                    # Cancel any ongoing response first
                    try:
                        cancel_response = {"type": "response.cancel"}
                        await self.openai.ws.send(json.dumps(cancel_response))
                        self.logger.info("Cancelled any ongoing OpenAI response")
                        await asyncio.sleep(0.2)  # Brief pause for cancellation to process
                    except Exception as e:
                        self.logger.warning(f"Failed to cancel ongoing response: {e}")
                    
                    # Trigger OpenAI to respond with a goodbye message
                    goodbye_response = {
                        "type": "response.create",
                        "response": {
                            "modalities": ["audio", "text"],
                            "instructions": GOODBYE_RESPONSE_INSTRUCTIONS,
                        }
                    }
                    await self.openai.ws.send(json.dumps(goodbye_response))
                    self.logger.info("Goodbye message sent to OpenAI - waiting for it to complete")
                    
                    # Wait for OpenAI to finish speaking the goodbye message (max 15 seconds)
                    try:
                        await asyncio.wait_for(self.goodbye_response_complete.wait(), timeout=15.0)
                        self.logger.info("Goodbye response completed, proceeding to hang up")
                    except asyncio.TimeoutError:
                        self.logger.warning("Goodbye response timeout after 15s - hanging up anyway")
                        self.waiting_for_goodbye_response = False
                
                # Now hang up after the goodbye message
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

    async def _handle_ai_speech_finished(self):
        """Handle when OpenAI finishes speaking - reset silence timer with buffer."""
        # Check if we were waiting for a goodbye response
        if self.waiting_for_goodbye_response:
            self.logger.info("Goodbye response complete - signaling to hang up")
            self.goodbye_response_complete.set()
            self.waiting_for_goodbye_response = False
            return  # Don't reset timers during goodbye
        
        if self.call_active and self.state == CallState.CALL_ACTIVE:
            # Reset the silence timer to now - the buffer will be handled in the detection loop
            self.last_user_response_time = datetime.now()
            self.logger.info(f"AI finished speaking - reset silence timer to {self.last_user_response_time}")
            self.logger.info(f"Silence detection active: will prompt after {self.sleep_check_interval}s of silence (with 3s buffer)")
        else:
            self.logger.debug(f"AI finished speaking but call not active (call_active={self.call_active}, state={self.state})")

    async def _handle_wake_detected(self):
        """Handle wake keyword detection (legacy - now handled by _handle_user_speech)."""
        # This is kept for backward compatibility but wake detection is now
        # handled through the goodbye/end call logic in _handle_user_speech
        self.logger.info("Wake keyword detected (legacy handler)")

    async def _handle_hangup_request(self, event: dict):
        """Handle hangup request."""
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_name = channel.get("name", "")
        
        self.logger.info(f"Hangup requested for channel {channel_id} ({channel_name})")
        
        # If it's a Local channel hangup during an active call, check if PJSIP channel is still in Stasis
        # This handles cases where user hangs up and we only get Local channel hangups
        if channel_name.startswith("Local/") and self.call_active and self.current_channel_id:
            try:
                # Check if PJSIP channel is still in Stasis
                channel_info = await self.ari.get_channel(self.current_channel_id)
                if not channel_info or channel_info.get("state") != "Up":
                    self.logger.info(f"PJSIP channel {self.current_channel_id} no longer in Stasis - user hung up")
                    # Trigger cleanup as if PJSIP channel hung up
                    self.call_active = False
                    self.state = CallState.CALL_ENDED
                    self.cleanup_complete.clear()
                    
                    # IMMEDIATELY close OpenAI and stop bridge
                    if self.openai and self.openai.ws:
                        self.logger.info("Closing OpenAI connection IMMEDIATELY to stop audio...")
                        try:
                            await self.openai.close()
                        except Exception as e:
                            self.logger.error(f"Error closing OpenAI: {e}")
                    
                    if self.bridge:
                        self.logger.info("Stopping audio bridge IMMEDIATELY...")
                        try:
                            await self.bridge.stop()
                        except Exception as e:
                            self.logger.error(f"Error stopping audio bridge: {e}")
                        self.bridge = None
                    
                    await self._cleanup()
                    return
            except Exception as e:
                # Channel doesn't exist - user hung up
                self.logger.info(f"PJSIP channel {self.current_channel_id} not found: {e} - user hung up")
                self.call_active = False
                self.state = CallState.CALL_ENDED
                self.cleanup_complete.clear()
                
                # IMMEDIATELY close OpenAI and stop bridge
                if self.openai and self.openai.ws:
                    self.logger.info("Closing OpenAI connection IMMEDIATELY to stop audio...")
                    try:
                        await self.openai.close()
                    except Exception as e:
                        self.logger.error(f"Error closing OpenAI: {e}")
                
                if self.bridge:
                    self.logger.info("Stopping audio bridge IMMEDIATELY...")
                    try:
                        await self.bridge.stop()
                    except Exception as e:
                        self.logger.error(f"Error stopping audio bridge: {e}")
                    self.bridge = None
                
                await self._cleanup()
                return
        
        # If it's the PJSIP channel (the actual phone call), end the call immediately
        if channel_name.startswith("PJSIP/") and channel_id == self.current_channel_id:
            self.logger.info(f"PJSIP channel {channel_id} hangup requested - ending call immediately")
            self.call_active = False
            self.state = CallState.CALL_ENDED
            self.cleanup_complete.clear()  # Clear event so callback loop waits for cleanup
            
            # IMMEDIATELY close OpenAI to stop it from sending audio
            if self.openai and self.openai.ws:
                self.logger.info("Closing OpenAI connection IMMEDIATELY to stop audio...")
                try:
                    await self.openai.close()
                    self.logger.info("OpenAI connection closed")
                except Exception as e:
                    self.logger.error(f"Error closing OpenAI: {e}")
            
            # IMMEDIATELY stop audio bridge to stop sending packets
            if self.bridge:
                self.logger.info("Stopping audio bridge IMMEDIATELY to stop sending packets...")
                try:
                    await self.bridge.stop()
                    self.logger.info("Audio bridge stopped")
                except Exception as e:
                    self.logger.error(f"Error stopping audio bridge: {e}")
                # Clear bridge reference so cleanup doesn't try again
                self.bridge = None
            
            # Cancel sleep detection tasks immediately to prevent them from detecting "silence"
            current_task = asyncio.current_task()
            if self.sleep_check_task and self.sleep_check_task != current_task:
                self.logger.info("Cancelling sleep detection task immediately")
                self.sleep_check_task.cancel()
            if self.sleep_prompt_task and self.sleep_prompt_task != current_task:
                self.logger.info("Cancelling sleep prompt task immediately")
                self.sleep_prompt_task.cancel()
            
            # Do rest of cleanup (but OpenAI and bridge are already done)
            await self._cleanup()

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

        # Handle PJSIP channel destruction - this means the call ended
        # Check by ID, or if it's a PJSIP channel and we have an active call (bridge exists or call_active)
        # This handles cases where call_active was already set to False by another handler
        if (channel_id == self.current_channel_id or 
            (channel_name.startswith("PJSIP/") and (self.call_active or self.bridge is not None))):
            self.logger.info(f"PJSIP channel destroyed - call ended (user hung up)")
            self.call_active = False
            self.state = CallState.CALL_ENDED
            self.cleanup_complete.clear()  # Clear event so callback loop waits for cleanup
            
            # Log different causes for debugging
            if cause == 16:  # Normal clearing
                self.logger.info("Call ended normally (user hung up)")
            elif cause == 21:  # User busy
                self.logger.warning("User was busy - call not answered")
            elif cause == 27:  # Destination out of order
                self.logger.error("Destination out of order - check phone number")
            elif cause == 34:  # Circuit/channel congestion
                self.logger.error("Circuit congestion - trunk may be unavailable")
            else:
                self.logger.warning(f"Call ended with cause {cause}: {cause_txt}")
            
            # IMMEDIATELY close OpenAI to stop it from sending audio
            if self.openai and self.openai.ws:
                self.logger.info("Closing OpenAI connection IMMEDIATELY to stop audio...")
                try:
                    await self.openai.close()
                    self.logger.info("OpenAI connection closed")
                except Exception as e:
                    self.logger.error(f"Error closing OpenAI: {e}")
            
            # IMMEDIATELY stop audio bridge
            if self.bridge:
                self.logger.info("Stopping audio bridge IMMEDIATELY...")
                try:
                    await self.bridge.stop()
                    self.logger.info("Audio bridge stopped")
                except Exception as e:
                    self.logger.error(f"Error stopping audio bridge: {e}")
                self.bridge = None
            
            await self._cleanup()

    async def _end_call(self):
        """End the current call."""
        # Even if call_active was flipped False by some other handler, if we still have
        # a current_channel_id we should attempt to hang it up (goodbye + doorbell).
        if self.current_channel_id:
            # IMPORTANT: hang up FIRST. Stopping OpenAI / bridge can block and delay hangup otherwise.
            channel_id = self.current_channel_id

            self.logger.info("Ending call (user said goodbye with doorbell activated) - hanging up immediately")
            self.should_call_back = False  # Don't call back if ending normally
            self.call_active = False
            self.state = CallState.CALL_ENDED
            self.cleanup_complete.clear()  # let callback loop (if still running) wait for cleanup

            # Cancel sleep detection tasks immediately
            current_task = asyncio.current_task()
            if self.sleep_check_task and self.sleep_check_task != current_task:
                self.logger.info("Cancelling sleep detection task immediately")
                self.sleep_check_task.cancel()
            if self.sleep_prompt_task and self.sleep_prompt_task != current_task:
                self.logger.info("Cancelling sleep prompt task immediately")
                self.sleep_prompt_task.cancel()

            # Hang up the channel FIRST (bounded time)
            try:
                await asyncio.wait_for(self.ari.hangup_channel(channel_id), timeout=2.0)
                self.logger.info(f"Hangup sent for channel {channel_id}")
            except asyncio.TimeoutError:
                self.logger.warning(f"Timeout sending hangup for channel {channel_id} (continuing cleanup)")
            except Exception as e:
                self.logger.error(f"Error hanging up channel {channel_id}: {e}")

            # Then stop OpenAI / bridge + cleanup, but don't allow this to delay hangup
            async def _post_hangup_cleanup():
                # IMMEDIATELY close OpenAI to stop it from sending audio
                if self.openai and self.openai.ws:
                    self.logger.info("Closing OpenAI connection IMMEDIATELY to stop audio...")
                    try:
                        await asyncio.wait_for(self.openai.close(), timeout=2.0)
                        self.logger.info("OpenAI connection closed")
                    except asyncio.TimeoutError:
                        self.logger.warning("Timeout closing OpenAI (continuing cleanup)")
                    except Exception as e:
                        self.logger.error(f"Error closing OpenAI: {e}")

                # IMMEDIATELY stop audio bridge to stop sending packets
                if self.bridge:
                    self.logger.info("Stopping audio bridge IMMEDIATELY...")
                    try:
                        await asyncio.wait_for(self.bridge.stop(), timeout=3.0)
                        self.logger.info("Audio bridge stopped")
                    except asyncio.TimeoutError:
                        self.logger.warning("Timeout stopping audio bridge (continuing cleanup)")
                    except Exception as e:
                        self.logger.error(f"Error stopping audio bridge: {e}")
                    self.bridge = None  # Clear bridge reference so cleanup doesn't try again

                await self._cleanup()

            asyncio.create_task(_post_hangup_cleanup())

    async def _end_call_for_sleep(self):
        """End call because user appears to be sleeping (will call back)."""
        if self.current_channel_id and self.call_active:
            self.logger.info("Ending call due to sleep detection (will call back)")
            self.sleep_detected = True  # Signal to call-back loop to call again

            try:
                await self.ari.hangup_channel(self.current_channel_id)
            except Exception as e:
                self.logger.error(f"Error hanging up channel: {e}")

            self.call_active = False
            self.state = CallState.CALL_ENDED
            self.cleanup_complete.clear()  # Clear event so callback loop waits for cleanup
            
            # Clean up resources (audio bridge, OpenAI) before callback
            await self._cleanup()

    async def _cleanup(self):
        """Clean up resources gracefully."""
        # Prevent multiple cleanups
        if self.cleaning_up:
            self.logger.debug("Cleanup already in progress, skipping")
            return
        
        self.cleaning_up = True
        self.cleanup_complete.clear()  # Signal that cleanup is starting
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
                self.logger.info("Stopping audio bridge...")
                await self.bridge.stop()
                self.logger.info("Audio bridge stopped")
        except Exception as e:
            self.logger.error(f"Error stopping audio bridge: {e}", exc_info=True)

        try:
            if self.openai:
                self.logger.info("Closing OpenAI connection...")
                await self.openai.close()
                self.logger.info("OpenAI connection closed")
        except Exception as e:
            self.logger.error(f"Error closing OpenAI connection: {e}", exc_info=True)

        self.current_channel_id = None
        self.bridge = None
        self.last_user_response_time = None
        self.cleaning_up = False
        self.cleanup_complete.set()  # Signal that cleanup is complete
        self.logger.info("Cleanup complete")