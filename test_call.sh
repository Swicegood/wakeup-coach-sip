#!/bin/bash
# Simple test script to originate a call via ARI and verify Stasis integration

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}ARI Call Origination Test${NC}"
echo -e "${BLUE}================================${NC}"
echo

# Load .env file
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
    echo -e "${GREEN}✓${NC} Loaded .env file"
else
    echo -e "${RED}✗${NC} .env file not found"
    exit 1
fi

# Set defaults
ARI_HOST=${ARI_HOST:-localhost}
ARI_PORT=${ARI_PORT:-8088}
ARI_USERNAME=${ARI_USERNAME:-}
ARI_PASSWORD=${ARI_PASSWORD:-}
ARI_APP_NAME=${ARI_APP_NAME:-wakeup-coach}
TARGET_PHONE_NUMBER=${TARGET_PHONE_NUMBER:-}

if [ -z "$ARI_USERNAME" ] || [ -z "$ARI_PASSWORD" ]; then
    echo -e "${RED}✗${NC} ARI_USERNAME and ARI_PASSWORD must be set in .env"
    exit 1
fi

if [ -z "$TARGET_PHONE_NUMBER" ]; then
    echo -e "${RED}✗${NC} TARGET_PHONE_NUMBER must be set in .env"
    exit 1
fi

echo -e "${YELLOW}Configuration:${NC}"
echo "  ARI Host: $ARI_HOST:$ARI_PORT"
echo "  ARI Username: $ARI_USERNAME"
echo "  App Name: $ARI_APP_NAME"
echo "  Target Number: $TARGET_PHONE_NUMBER"
echo

# Test ARI connection
echo -e "${BLUE}Testing ARI connection...${NC}"
ARI_TEST=$(curl -s -w "\n%{http_code}" -u "$ARI_USERNAME:$ARI_PASSWORD" \
    "http://$ARI_HOST:$ARI_PORT/ari/asterisk/info" 2>&1)
ARI_HTTP_CODE=$(echo "$ARI_TEST" | tail -n1)
ARI_RESPONSE=$(echo "$ARI_TEST" | sed '$d')

if echo "$ARI_RESPONSE" | grep -q "entity_id"; then
    echo -e "${GREEN}✓${NC} ARI connection successful"
elif [ "$ARI_HTTP_CODE" = "401" ]; then
    echo -e "${RED}✗${NC} ARI authentication failed (401)"
    echo "   Check your ARI_USERNAME and ARI_PASSWORD in .env"
    exit 1
else
    echo -e "${YELLOW}⚠${NC} ARI connection test failed (HTTP $ARI_HTTP_CODE)"
    echo "   Continuing anyway..."
fi
echo

# Originate the call
echo -e "${BLUE}Originating call...${NC}"
# Use Local channel to route through dialplan (matches production code)
DIAL_NUMBER=$(echo "$TARGET_PHONE_NUMBER" | sed 's/^+//')  # Remove leading +
ENDPOINT="Local/$DIAL_NUMBER@from-internal"
echo "  Endpoint: $ENDPOINT"
echo "  App: $ARI_APP_NAME"
echo "  Note: Call will route through dialplan to Stasis(wakeup-coach)"
echo

RESPONSE=$(curl -s -w "\n%{http_code}" -u "$ARI_USERNAME:$ARI_PASSWORD" \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{
        \"endpoint\": \"$ENDPOINT\",
        \"app\": \"$ARI_APP_NAME\",
        \"appArgs\": \"test\",
        \"callerId\": \"Test Call\"
    }" \
    "http://$ARI_HOST:$ARI_PORT/ari/channels")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}✓${NC} Call originated successfully"
    CHANNEL_ID=$(echo "$BODY" | grep -o '"id":"[^"]*' | head -1 | cut -d'"' -f4)
    echo "  Channel ID: $CHANNEL_ID"
    echo
    echo -e "${YELLOW}Next steps:${NC}"
    echo "1. Check application logs for StasisStart event"
    echo "2. Monitor Asterisk logs:"
    echo "   ${BLUE}tail -f /var/log/asterisk/full | grep -i stasis${NC}"
    echo "3. Watch for StasisStart event in your application"
    echo
    echo -e "${GREEN}✅ Test call originated!${NC}"
    echo "   The call should now enter Stasis and trigger StasisStart event"
else
    echo -e "${RED}✗${NC} Failed to originate call"
    echo "  HTTP Code: $HTTP_CODE"
    echo "  Response: $BODY"
    exit 1
fi
