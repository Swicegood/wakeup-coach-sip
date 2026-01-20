"""HTTP server for doorbell webhook endpoint."""

import asyncio
import json
import logging
from typing import Optional
from datetime import datetime, timedelta
from aiohttp import web


class DoorbellWebhook:
    """Manages doorbell activation state and webhook endpoint."""

    def __init__(self, logger: logging.Logger, timeout_minutes: int = 5):
        """
        Initialize doorbell webhook handler.

        Args:
            logger: Logger instance
            timeout_minutes: Minutes until doorbell activation expires (default: 5)
        """
        self.logger = logger
        self.timeout_minutes = timeout_minutes
        self.doorbell_activated = False
        self.doorbell_activation_time: Optional[datetime] = None
        self.doorbell_timeout_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def activate(self):
        """Activate doorbell flag and schedule timeout."""
        async with self._lock:
            self.doorbell_activated = True
            self.doorbell_activation_time = datetime.now()
            
            # Cancel existing timeout task if any
            if self.doorbell_timeout_task:
                self.doorbell_timeout_task.cancel()
            
            # Schedule timeout
            self.doorbell_timeout_task = asyncio.create_task(self._timeout_after_delay())
            
            self.logger.info(
                f"Doorbell activated! Will expire in {self.timeout_minutes} minutes "
                f"(at {self.doorbell_activation_time + timedelta(minutes=self.timeout_minutes)})"
            )

    async def _timeout_after_delay(self):
        """Reset doorbell flag after timeout period."""
        try:
            await asyncio.sleep(self.timeout_minutes * 60)
            async with self._lock:
                self.doorbell_activated = False
                self.doorbell_activation_time = None
                self.logger.info(f"Doorbell activation expired after {self.timeout_minutes} minutes")
        except asyncio.CancelledError:
            # Timeout was cancelled (new activation occurred)
            pass

    def is_activated(self) -> bool:
        """Check if doorbell is currently activated."""
        return self.doorbell_activated

    def get_activation_time(self) -> Optional[datetime]:
        """Get when doorbell was last activated."""
        return self.doorbell_activation_time

    async def handle_webhook(self, request: web.Request) -> web.Response:
        """
        Handle POST request to /doorbell-webhook.

        Args:
            request: HTTP request

        Returns:
            HTTP response
        """
        self.logger.info(f"Doorbell webhook endpoint hit: method={request.method}, path={request.path}, remote={request.remote}")
        try:
            # Try to parse request body if present (optional)
            data = {}
            try:
                data = await request.json()
            except (json.JSONDecodeError, ValueError, TypeError):
                # If not JSON or no body, that's fine - we'll still activate
                try:
                    body = await request.read()
                    if body:
                        data = json.loads(body.decode('utf-8'))
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    # Empty body or invalid JSON is fine
                    pass
            
            event_type = data.get('event_type', '').lower() if data else ''
            device_id = data.get('device_id', '') if data else ''
            
            self.logger.info(f"Doorbell webhook received: event_type={event_type}, device_id={device_id}")
            if data:
                self.logger.debug(f"Webhook payload: {data}")
            
            # Activate doorbell (any POST to this endpoint activates it)
            await self.activate()
            
            return web.json_response({
                "status": "success",
                "message": "Doorbell activated",
                "expires_in_minutes": self.timeout_minutes
            })
            
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in webhook request: {e}")
            return web.json_response(
                {"status": "error", "message": "Invalid JSON"},
                status=400
            )
        except Exception as e:
            self.logger.error(f"Error handling doorbell webhook: {e}", exc_info=True)
            return web.json_response(
                {"status": "error", "message": "Internal server error"},
                status=500
            )

    def create_app(self) -> web.Application:
        """Create aiohttp application with doorbell webhook route."""
        app = web.Application()
        # Support both endpoint names for flexibility
        app.router.add_post('/doorbell-webhook', self.handle_webhook)
        app.router.add_post('/activate-doorbell', self.handle_webhook)
        return app
