#!/bin/bash
# Test ARI originate with custom dialplan extension

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}Testing ARI with dialplan Stasis extension...${NC}"
echo

# Load .env
if [ -f .env ]; then
    source .env
else
    echo "Error: .env file not found"
    exit 1
fi

# Test originate to the dialplan extension
echo -e "${YELLOW}Originating call to Local/1000@ari-test${NC}"
echo "  This should route through dialplan to Stasis(wakeup-coach)"
echo

RESPONSE=$(curl -s -w "\n%{http_code}" -u "$ARI_USERNAME:$ARI_PASSWORD" \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{
        \"endpoint\": \"Local/1000@ari-test\",
        \"app\": \"wakeup-coach\",
        \"appArgs\": \"test\",
        \"callerId\": \"Test Call\"
    }" \
    "http://$ARI_HOST:$ARI_PORT/ari/channels")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}✓ Call originated successfully${NC}"
    CHANNEL_ID=$(echo "$BODY" | grep -o '"id":"[^"]*' | head -1 | cut -d'"' -f4)
    echo "  Channel ID: $CHANNEL_ID"
    echo
    echo "Check Asterisk logs for StasisStart event:"
    echo "  tail -f /var/log/asterisk/full | grep -i 'stasis\|$CHANNEL_ID'"
else
    echo "Failed: HTTP $HTTP_CODE"
    echo "$BODY"
fi
