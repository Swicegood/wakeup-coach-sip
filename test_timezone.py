#!/usr/bin/env python3
"""Test timezone functionality."""

import sys
import os
from dotenv import load_dotenv

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

from config import load_config
from datetime import datetime, time as dt_time, timedelta

# Try to use zoneinfo (Python 3.9+), fallback to pytz
try:
    from zoneinfo import ZoneInfo
    HAS_ZONEINFO = True
    print("✓ Using zoneinfo (built-in)")
except ImportError:
    try:
        import pytz
        HAS_ZONEINFO = False
        print("✓ Using pytz (fallback)")
    except ImportError:
        print("✗ ERROR: Neither zoneinfo nor pytz available!")
        sys.exit(1)

def test_timezone():
    """Test timezone configuration."""
    # Load config
    os.environ.clear()
    load_dotenv('.env_prod')
    
    try:
        config = load_config()
        print(f"\n✓ Config loaded successfully")
        print(f"  TIMEZONE: {config.call.timezone}")
        print(f"  WAKE_UP_TIME: {config.call.wake_up_time}")
        print(f"  CALL_IMMEDIATELY: {config.call.call_immediately}")
        
        if not config.call.wake_up_time:
            print("\n⚠ No WAKE_UP_TIME configured")
            return
        
        # Parse wake-up time
        wake_hour, wake_minute = map(int, config.call.wake_up_time.split(":"))
        wake_time = dt_time(wake_hour, wake_minute)
        
        # Get timezone
        try:
            if HAS_ZONEINFO:
                tz = ZoneInfo(config.call.timezone)
                utc_tz = ZoneInfo("UTC")
            else:
                import pytz
                tz = pytz.timezone(config.call.timezone)
                utc_tz = pytz.UTC
            print(f"\n✓ Timezone loaded: {config.call.timezone}")
        except Exception as e:
            print(f"\n✗ ERROR loading timezone: {e}")
            return
        
        # Get current time
        now = datetime.now(tz)
        
        # Create wake datetime
        if HAS_ZONEINFO:
            wake_datetime = datetime.combine(now.date(), wake_time, tz)
        else:
            import pytz
            naive_wake = datetime.combine(now.date(), wake_time)
            wake_datetime = tz.localize(naive_wake)
        
        # If wake time has already passed today, schedule for tomorrow
        if wake_datetime <= now:
            wake_datetime += timedelta(days=1)
        
        wait_seconds = (wake_datetime - now).total_seconds()
        wait_hours = wait_seconds / 3600
        
        # Log times
        now_utc = now.astimezone(utc_tz)
        wake_utc = wake_datetime.astimezone(utc_tz)
        
        print(f"\n📅 Current time ({config.call.timezone}): {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"📅 Current time (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"⏰ Scheduled wake-up ({config.call.timezone}): {wake_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"⏰ Scheduled wake-up (UTC): {wake_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"⏳ Waiting {wait_hours:.2f} hours ({wait_seconds:.0f} seconds)")
        
        print("\n✓ Timezone test passed!")
        
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    test_timezone()
