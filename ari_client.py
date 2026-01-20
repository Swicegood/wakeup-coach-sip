"""Asterisk ARI client for call control and audio streaming."""

import asyncio
import json
import logging
from typing import Optional, Callable, Dict, Any

import aiohttp
import websockets
from websockets.client import WebSocketClientProtocol

from config import AsteriskConfig


class ARIClient:
    """Client for interacting with Asterisk REST Interface."""

    def __init__(self, config: AsteriskConfig, logger: logging.Logger):
        """
        Initialize ARI client.

        Args:
            config: Asterisk configuration
            logger: Logger instance
        """
        self.config = config
        self.logger = logger
        self.ws: Optional[WebSocketClientProtocol] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.channel_id: Optional[str] = None
        self.event_handlers: Dict[str, Callable] = {}

    async def __aenter__(self):
        """Async context manager entry."""
        auth = aiohttp.BasicAuth(self.config.username, self.config.password)
        self.session = aiohttp.ClientSession(auth=auth)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()

    def on_event(self, event_type: str, handler: Callable):
        """
        Register an event handler.

        Args:
            event_type: ARI event type (e.g., 'StasisStart', 'ChannelHangupRequest')
            handler: Async function to handle the event
        """
        self.event_handlers[event_type] = handler

    async def connect_websocket(self):
        """Connect to ARI WebSocket for event stream."""
        ws_url = (
            f"ws://{self.config.host}:{self.config.port}/ari/events"
            f"?app={self.config.app_name}&api_key={self.config.username}:{self.config.password}"
        )

        self.logger.info(f"Connecting to ARI WebSocket for app '{self.config.app_name}'")
        self.logger.debug(f"WebSocket URL: ws://{self.config.host}:{self.config.port}/ari/events?app={self.config.app_name}")

        try:
            self.ws = await websockets.connect(ws_url)
            self.logger.info(f"Connected to ARI WebSocket - app '{self.config.app_name}' is now registered")
            
            # Verify registration by listing apps
            await self._verify_app_registered()
            
        except Exception as e:
            self.logger.error(f"Failed to connect to ARI WebSocket: {e}")
            raise

    async def _verify_app_registered(self):
        """Verify our app is registered in ARI."""
        try:
            url = f"{self.config.base_url}/applications"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    apps = await resp.json()
                    app_names = [app.get("name") for app in apps]
                    if self.config.app_name in app_names:
                        self.logger.info(f"✓ App '{self.config.app_name}' verified in ARI: {app_names}")
                    else:
                        self.logger.error(f"✗ App '{self.config.app_name}' NOT in ARI apps: {app_names}")
                else:
                    self.logger.warning(f"Could not verify app registration: {resp.status}")
        except Exception as e:
            self.logger.warning(f"Could not verify app registration: {e}")

    async def listen_for_events(self):
        """Listen for events from ARI WebSocket with auto-reconnect."""
        while True:
            if not self.ws:
                self.logger.info("WebSocket not connected, connecting...")
                try:
                    await self.connect_websocket()
                except Exception as e:
                    self.logger.error(f"Failed to connect WebSocket: {e}")
                    await asyncio.sleep(2)
                    continue

            self.logger.info("Listening for ARI events...")

            try:
                async for message in self.ws:
                    try:
                        event = json.loads(message)
                        event_type = event.get("type")

                        self.logger.debug(f"Received ARI event: {event_type}")
                        
                        # Log StasisEnd events at INFO level to debug
                        if event_type == "StasisEnd":
                            self.logger.info(f"StasisEnd event received: {event}")

                        if event_type in self.event_handlers:
                            self.logger.debug(f"Calling handler for {event_type}")
                            await self.event_handlers[event_type](event)
                        else:
                            self.logger.debug(f"No handler for event type: {event_type}")

                    except json.JSONDecodeError as e:
                        self.logger.error(f"Failed to parse ARI event: {e}")
                    except Exception as e:
                        self.logger.error(f"Error handling ARI event: {e}", exc_info=True)

            except websockets.exceptions.ConnectionClosed:
                self.logger.warning("ARI WebSocket connection closed - will reconnect")
                self.ws = None
                await asyncio.sleep(1)
            except Exception as e:
                self.logger.error(f"Error in event listener: {e}", exc_info=True)
                self.ws = None
                await asyncio.sleep(1)

    async def originate_call(
        self, 
        endpoint: str, 
        caller_id: str = "WakeUpCoach",
        extension: str = None,
        context: str = None
    ) -> Dict[str, Any]:
        """
        Originate a new call.

        Args:
            endpoint: Endpoint to call (e.g., 'Local/number@context')
            caller_id: Caller ID to use
            extension: Dialplan extension to execute
            context: Dialplan context for the extension

        Returns:
            Channel information
        """
        url = f"{self.config.base_url}/channels"

        payload = {
            "endpoint": endpoint,
            "callerId": caller_id,
            "app": self.config.app_name,  # Required for ARI to track channel events
        }
        
        # Add extension and context for dialplan routing
        if extension:
            payload["extension"] = extension
        if context:
            payload["context"] = context
            
        self.logger.info(f"Originating call: endpoint={endpoint}, extension={extension}@{context}")

        async with self.session.post(url, json=payload) as resp:
            if resp.status == 200:
                channel = await resp.json()
                self.channel_id = channel.get("id")
                self.logger.info(f"Call originated: channel_id={self.channel_id}")
                return channel
            else:
                error = await resp.text()
                self.logger.error(f"Failed to originate call: {resp.status} - {error}")
                raise RuntimeError(f"Failed to originate call: {error}")

    async def answer_channel(self, channel_id: str):
        """
        Answer a channel.

        Args:
            channel_id: Channel ID to answer
        """
        url = f"{self.config.base_url}/channels/{channel_id}/answer"

        self.logger.info(f"Answering channel: {channel_id}")

        async with self.session.post(url) as resp:
            if resp.status == 204:
                self.logger.info(f"Channel answered: {channel_id}")
            else:
                error = await resp.text()
                self.logger.error(f"Failed to answer channel: {resp.status} - {error}")

    async def create_external_media(
        self, external_host: str, format: str = "slin16"
    ) -> Dict[str, Any]:
        """
        Create an external media channel for audio streaming.

        Args:
            external_host: External host for media streaming (host:port)
            format: Audio format (default: slin16 = 16kHz signed linear)

        Returns:
            External media channel information
        """
        # External media API uses query parameters
        url = f"{self.config.base_url}/channels/externalMedia"

        params = {
            "app": self.config.app_name,
            "external_host": external_host,
            "format": format,
            "encapsulation": "rtp",
            "transport": "udp",
            "connection_type": "client",
            "direction": "both"
        }

        self.logger.info(f"Creating external media channel with format {format}")

        async with self.session.post(url, params=params) as resp:
            if resp.status == 200:
                channel = await resp.json()
                self.logger.info(f"External media channel created: {channel.get('id')}")
                return channel
            else:
                error = await resp.text()
                self.logger.error(f"Failed to create external media: {resp.status} - {error}")
                raise RuntimeError(f"Failed to create external media: {error}")

    async def hangup_channel(self, channel_id: str):
        """
        Hangup a channel.

        Args:
            channel_id: Channel ID to hangup
        """
        url = f"{self.config.base_url}/channels/{channel_id}"

        self.logger.info(f"Hanging up channel: {channel_id}")

        async with self.session.delete(url) as resp:
            if resp.status in (204, 404):
                self.logger.info(f"Channel hung up: {channel_id}")
            else:
                error = await resp.text()
                self.logger.error(f"Failed to hangup channel: {resp.status} - {error}")

    async def get_channel(self, channel_id: str) -> Dict[str, Any]:
        """
        Get channel information.

        Args:
            channel_id: Channel ID

        Returns:
            Channel information
        """
        url = f"{self.config.base_url}/channels/{channel_id}"

        async with self.session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                error = await resp.text()
                self.logger.error(f"Failed to get channel: {resp.status} - {error}")
                raise RuntimeError(f"Failed to get channel: {error}")

    async def create_bridge(self, bridge_type: str = "mixing") -> Dict[str, Any]:
        """
        Create a bridge for mixing audio channels.

        Args:
            bridge_type: Type of bridge (mixing, holding, dtmf_events, proxy_media)

        Returns:
            Bridge information
        """
        url = f"{self.config.base_url}/bridges"

        payload = {
            "type": bridge_type
        }

        self.logger.info(f"Creating {bridge_type} bridge")

        async with self.session.post(url, json=payload) as resp:
            if resp.status == 200:
                bridge = await resp.json()
                self.logger.info(f"Bridge created: {bridge.get('id')}")
                return bridge
            else:
                error = await resp.text()
                self.logger.error(f"Failed to create bridge: {resp.status} - {error}")
                raise RuntimeError(f"Failed to create bridge: {error}")

    async def add_channel_to_bridge(self, bridge_id: str, channel_id: str):
        """
        Add a channel to a bridge.

        Args:
            bridge_id: Bridge ID
            channel_id: Channel ID to add
        """
        url = f"{self.config.base_url}/bridges/{bridge_id}/addChannel"

        payload = {
            "channel": channel_id
        }

        self.logger.info(f"Adding channel {channel_id} to bridge {bridge_id}")

        async with self.session.post(url, json=payload) as resp:
            if resp.status == 204:
                self.logger.info(f"Channel added to bridge")
            else:
                error = await resp.text()
                self.logger.error(f"Failed to add channel to bridge: {resp.status} - {error}")
                raise RuntimeError(f"Failed to add channel to bridge: {error}")

    async def delete_bridge(self, bridge_id: str):
        """
        Delete a bridge.

        Args:
            bridge_id: Bridge ID to delete
        """
        url = f"{self.config.base_url}/bridges/{bridge_id}"

        self.logger.info(f"Deleting bridge: {bridge_id}")

        async with self.session.delete(url) as resp:
            if resp.status in (204, 404):
                self.logger.info(f"Bridge deleted: {bridge_id}")
            else:
                error = await resp.text()
                self.logger.error(f"Failed to delete bridge: {resp.status} - {error}")
