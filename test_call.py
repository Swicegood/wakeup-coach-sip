#!/usr/bin/env python3
"""Simple test script to originate a call and verify Stasis integration."""

import asyncio
import sys
from dotenv import load_dotenv

from config import load_config
from logger import setup_logging
from ari_client import ARIClient


async def test_call():
    """Test originating a call via ARI."""
    # Load environment variables
    load_dotenv()

    # Load configuration
    try:
        config = load_config()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    # Setup logging
    logger = setup_logging(config.log_level)
    
    logger.info("=" * 60)
    logger.info("ARI Call Origination Test")
    logger.info("=" * 60)
    logger.info(f"Target phone: {config.call.target_phone_number}")
    logger.info(f"Asterisk ARI: {config.asterisk.base_url}")
    logger.info(f"App name: {config.asterisk.app_name}")
    logger.info("=" * 60)

    # Create ARI client
    ari = ARIClient(config.asterisk, logger)
    
    stasis_started = asyncio.Event()
    channel_id_holder = {"id": None}

    def handle_stasis_start(event: dict):
        """Handle StasisStart event."""
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_id_holder["id"] = channel_id
        logger.info(f"✅ SUCCESS: StasisStart received! Channel ID: {channel_id}")
        logger.info(f"   Channel state: {channel.get('state')}")
        logger.info(f"   Channel name: {channel.get('name')}")
        stasis_started.set()

    async with ari:
        # Connect to WebSocket
        logger.info("Connecting to ARI WebSocket...")
        await ari.connect_websocket()
        logger.info("✅ Connected to ARI WebSocket")
        
        # Register StasisStart handler
        ari.on_event("StasisStart", handle_stasis_start)
        
        # Start event listener in background
        listener_task = asyncio.create_task(ari.listen_for_events())
        
        # Wait a moment for WebSocket to be ready
        await asyncio.sleep(1)
        
        # Originate the call
        logger.info("Originating test call...")
        endpoint = f"PJSIP/{config.call.target_phone_number}@voipms"
        logger.info(f"Endpoint: {endpoint}")
        
        try:
            channel = await ari.originate_call(
                endpoint=endpoint,
                caller_id="Test Call"
            )
            logger.info(f"✅ Call originated successfully")
            logger.info(f"   Channel ID: {channel.get('id')}")
            logger.info(f"   Channel state: {channel.get('state')}")
            
            # Wait for StasisStart event (with timeout)
            logger.info("Waiting for StasisStart event (timeout: 30 seconds)...")
            try:
                await asyncio.wait_for(stasis_started.wait(), timeout=30.0)
                logger.info("=" * 60)
                logger.info("✅ TEST PASSED: Call entered Stasis successfully!")
                logger.info("=" * 60)
            except asyncio.TimeoutError:
                logger.error("=" * 60)
                logger.error("❌ TEST FAILED: StasisStart event not received within 30 seconds")
                logger.error("   This means the call did not enter Stasis")
                logger.error("   Check:")
                logger.error("   1. App name matches exactly")
                logger.error("   2. Endpoint format is correct")
                logger.error("   3. Asterisk logs for errors")
                logger.error("=" * 60)
                
                # Try to get channel status
                if channel.get('id'):
                    logger.info(f"Channel ID was: {channel.get('id')}")
                    logger.info("Check Asterisk logs: tail -f /var/log/asterisk/full | grep -i stasis")
                
        except Exception as e:
            logger.error(f"❌ Failed to originate call: {e}", exc_info=True)
            sys.exit(1)
        finally:
            # Cancel listener task
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(test_call())
    except KeyboardInterrupt:
        print("\nTest interrupted by user")
        sys.exit(1)
