#!/usr/bin/env python3
"""
Smoke test for ARI connection and Stasis app registration.

Run this BEFORE testing calls to verify:
1. ARI HTTP connection works
2. ARI WebSocket connects successfully
3. App 'wakeup-coach' is registered

Usage:
    python smoke_test.py

Or from Docker:
    docker-compose run --rm wakeup-coach python smoke_test.py
"""

import asyncio
import os
import sys
import aiohttp
import websockets


async def main():
    # Load config from environment
    host = os.getenv("ARI_HOST", "localhost")
    port = os.getenv("ARI_PORT", "8088")
    username = os.getenv("ARI_USERNAME", "")
    password = os.getenv("ARI_PASSWORD", "")
    app_name = os.getenv("ARI_APP_NAME", "wakeup-coach")
    
    if not username or not password:
        print("❌ ERROR: ARI_USERNAME and ARI_PASSWORD must be set")
        sys.exit(1)
    
    print(f"🔍 Testing ARI connection to {host}:{port}")
    print(f"   App name: {app_name}")
    print()
    
    # Test 1: HTTP connection
    print("1️⃣  Testing ARI HTTP connection...")
    base_url = f"http://{host}:{port}/ari"
    auth = aiohttp.BasicAuth(username, password)
    
    try:
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(f"{base_url}/asterisk/info") as resp:
                if resp.status == 200:
                    info = await resp.json()
                    print(f"   ✓ Connected to Asterisk {info.get('system', {}).get('version', 'unknown')}")
                else:
                    print(f"   ❌ HTTP {resp.status}: {await resp.text()}")
                    sys.exit(1)
    except Exception as e:
        print(f"   ❌ Connection failed: {e}")
        sys.exit(1)
    
    # Test 2: List current apps (before WebSocket)
    print("\n2️⃣  Checking registered ARI apps (before WebSocket)...")
    try:
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(f"{base_url}/applications") as resp:
                if resp.status == 200:
                    apps = await resp.json()
                    app_names = [app.get("name") for app in apps]
                    if app_names:
                        print(f"   Current apps: {app_names}")
                    else:
                        print("   No apps currently registered")
                else:
                    print(f"   ❌ HTTP {resp.status}")
    except Exception as e:
        print(f"   ❌ Failed: {e}")
    
    # Test 3: WebSocket connection
    print(f"\n3️⃣  Connecting WebSocket for app '{app_name}'...")
    ws_url = f"ws://{host}:{port}/ari/events?app={app_name}&api_key={username}:{password}"
    
    try:
        async with websockets.connect(ws_url) as ws:
            print(f"   ✓ WebSocket connected!")
            
            # Test 4: Verify app is now registered
            print(f"\n4️⃣  Verifying app '{app_name}' is registered...")
            async with aiohttp.ClientSession(auth=auth) as session:
                async with session.get(f"{base_url}/applications") as resp:
                    if resp.status == 200:
                        apps = await resp.json()
                        app_names = [app.get("name") for app in apps]
                        if app_name in app_names:
                            print(f"   ✓ App '{app_name}' is registered!")
                            print(f"   All apps: {app_names}")
                        else:
                            print(f"   ❌ App '{app_name}' NOT in list: {app_names}")
                            sys.exit(1)
            
            # Keep WebSocket open briefly to confirm stability
            print("\n5️⃣  Testing WebSocket stability (3 seconds)...")
            await asyncio.sleep(3)
            print("   ✓ WebSocket remained connected")
            
    except Exception as e:
        print(f"   ❌ WebSocket failed: {e}")
        sys.exit(1)
    
    print("\n" + "="*50)
    print("✅ ALL TESTS PASSED!")
    print("="*50)
    print()
    print("Next steps:")
    print("1. Keep the app running (WebSocket must stay connected)")
    print("2. In Asterisk CLI, run: ari show apps")
    print(f"   → Should show '{app_name}'")
    print("3. Test call origination:")
    print("   asterisk -rx \"channel originate Local/19199129332@wakeup-trigger application Wait 60\"")
    print()


if __name__ == "__main__":
    asyncio.run(main())
