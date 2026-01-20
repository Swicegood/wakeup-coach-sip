"""Main entry point for Wake-Up Coach service."""

import asyncio
import sys
from dotenv import load_dotenv

from config import load_config
from logger import setup_logging
from call_manager import CallManager


async def main():
    """Main application entry point."""
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
    logger.info("Wake-Up Coach Service Starting")
    logger.info("=" * 60)
    logger.info(f"Target phone: {config.call.target_phone_number}")
    logger.info(f"Wake keyword: {config.call.wake_keyword}")
    logger.info(f"Asterisk ARI: {config.asterisk.base_url}")
    logger.info(f"OpenAI Model: {config.openai.model}")
    logger.info("=" * 60)

    # Create and start call manager
    call_manager = CallManager(config, logger)

    try:
        await call_manager.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Wake-Up Coach Service stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete")
