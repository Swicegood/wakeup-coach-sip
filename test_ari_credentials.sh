#!/bin/bash

# ARI Credentials Test Script
# This script helps you verify your Asterisk ARI credentials

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "================================"
echo "ARI Credentials Test"
echo "================================"
echo

# Load .env file if it exists
if [ -f .env ]; then
    source .env
    echo -e "${GREEN}✓${NC} Found .env file"
else
    echo -e "${RED}✗${NC} .env file not found"
    echo "Please create .env from .env.example first"
    exit 1
fi

# Check required variables
echo
echo "Checking configuration..."
echo "ARI_HOST: $ARI_HOST"
echo "ARI_PORT: $ARI_PORT"
echo "ARI_USERNAME: $ARI_USERNAME"
echo "ARI_PASSWORD: ${ARI_PASSWORD:0:10}..." # Show only first 10 chars

# Check if password looks like a hash
if [[ $ARI_PASSWORD == \$6\$* ]]; then
    echo
    echo -e "${RED}✗ ERROR: Your ARI_PASSWORD appears to be a HASHED password${NC}"
    echo
    echo "The password in your .env file starts with \$6\$, which indicates it's a"
    echo "SHA-512 crypt hash. ARI authentication requires the PLAINTEXT password."
    echo
    echo "Solutions:"
    echo "1. Check the FreePBX web interface for the plaintext password"
    echo "2. Reset the password in /etc/asterisk/ari.conf on the Asterisk server"
    echo "3. See TROUBLESHOOTING.md for detailed instructions"
    echo
    exit 1
fi

# Test connectivity
echo
echo "Testing connectivity to $ARI_HOST:$ARI_PORT..."
if timeout 5 bash -c "cat < /dev/null > /dev/tcp/$ARI_HOST/$ARI_PORT" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Can connect to $ARI_HOST:$ARI_PORT"
else
    echo -e "${RED}✗${NC} Cannot connect to $ARI_HOST:$ARI_PORT"
    echo "Please check:"
    echo "  - Asterisk is running"
    echo "  - HTTP server is enabled in /etc/asterisk/http.conf"
    echo "  - Firewall allows connections to port $ARI_PORT"
    exit 1
fi

# Test HTTP endpoint
echo
echo "Testing ARI HTTP endpoint..."
RESPONSE=$(curl -s -u "$ARI_USERNAME:$ARI_PASSWORD" \
    "http://$ARI_HOST:$ARI_PORT/ari/asterisk/info" 2>&1)

if echo "$RESPONSE" | grep -q "Authentication required"; then
    echo -e "${RED}✗${NC} Authentication FAILED"
    echo
    echo "The credentials are incorrect. Response:"
    echo "$RESPONSE"
    echo
    echo "Please verify:"
    echo "  1. Username is correct (currently: $ARI_USERNAME)"
    echo "  2. Password is the PLAINTEXT password (not hashed)"
    echo "  3. User exists in /etc/asterisk/ari.conf"
    echo
    echo "See TROUBLESHOOTING.md for help"
    exit 1
elif echo "$RESPONSE" | grep -q "asterisk_id"; then
    echo -e "${GREEN}✓${NC} Authentication SUCCESSFUL"
    echo
    echo "Asterisk info:"
    echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
else
    echo -e "${YELLOW}?${NC} Unexpected response:"
    echo "$RESPONSE"
    exit 1
fi

# Test WebSocket endpoint
echo
echo "Testing ARI WebSocket endpoint..."
WS_URL="ws://$ARI_HOST:$ARI_PORT/ari/events?app=$ARI_APP_NAME&api_key=$ARI_USERNAME:$ARI_PASSWORD"

# Use Python to test WebSocket
python3 << EOF
import asyncio
import websockets
import sys

async def test_websocket():
    uri = "$WS_URL"
    try:
        async with websockets.connect(uri, timeout=5) as ws:
            print("${GREEN}✓${NC} WebSocket connection successful")
            return 0
    except websockets.exceptions.InvalidStatus as e:
        print("${RED}✗${NC} WebSocket connection failed:", e)
        return 1
    except Exception as e:
        print("${YELLOW}?${NC} Error testing WebSocket:", e)
        return 1

sys.exit(asyncio.run(test_websocket()))
EOF

WS_RESULT=$?

echo
if [ $WS_RESULT -eq 0 ]; then
    echo "================================"
    echo -e "${GREEN}All tests passed!${NC}"
    echo "================================"
    echo
    echo "Your ARI credentials are working correctly."
    echo "You can now run: docker-compose up -d"
else
    echo "================================"
    echo -e "${RED}WebSocket test failed${NC}"
    echo "================================"
    echo
    echo "HTTP authentication works, but WebSocket connection failed."
    echo "This might be a temporary issue. Try running the service anyway."
fi
EOF
