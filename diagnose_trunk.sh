#!/bin/bash

# Trunk Diagnostic Script
# Helps diagnose trunk and call routing issues

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}Trunk Diagnostic Tool${NC}"
echo -e "${BLUE}================================${NC}"
echo

# Load .env
if [ -f .env ]; then
    source .env
    echo -e "${GREEN}✓${NC} Loaded .env file"
else
    echo -e "${RED}✗${NC} .env file not found"
    exit 1
fi

echo
echo -e "${YELLOW}Current Configuration:${NC}"
echo "  Target Number: $TARGET_PHONE_NUMBER"
echo "  ARI Host: $ARI_HOST"
echo

# Check if we can reach Asterisk HTTP
echo -e "${BLUE}Testing Asterisk HTTP connectivity...${NC}"
if curl -s -u "$ARI_USERNAME:$ARI_PASSWORD" \
    "http://$ARI_HOST:$ARI_PORT/ari/asterisk/info" | grep -q "asterisk_id"; then
    echo -e "${GREEN}✓${NC} Asterisk HTTP reachable"
else
    echo -e "${RED}✗${NC} Cannot reach Asterisk HTTP"
    exit 1
fi

# List available endpoints
echo
echo -e "${BLUE}Available PJSIP Endpoints:${NC}"
curl -s -u "$ARI_USERNAME:$ARI_PASSWORD" \
    "http://$ARI_HOST:$ARI_PORT/ari/endpoints" | \
    python3 -m json.tool | grep -E '"resource"|"state"' | \
    paste - - | sed 's/"resource": //g' | sed 's/"state": //g' | \
    sed 's/[",]//g' | awk '{printf "  %-20s %s\n", $1, $2}'

echo
echo -e "${BLUE}Checking voipms trunk specifically:${NC}"
VOIPMS_STATUS=$(curl -s -u "$ARI_USERNAME:$ARI_PASSWORD" \
    "http://$ARI_HOST:$ARI_PORT/ari/endpoints/PJSIP/voipms" 2>&1)

if echo "$VOIPMS_STATUS" | grep -q '"state"'; then
    STATE=$(echo "$VOIPMS_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])")
    echo -e "  Status: ${GREEN}$STATE${NC}"
else
    echo -e "  Status: ${RED}Error or Not Found${NC}"
    echo "  Response: $VOIPMS_STATUS"
fi

echo
echo -e "${YELLOW}Diagnostic Summary:${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "The call is originating successfully but immediately failing with:"
echo -e "${RED}  Circuit/channel congestion${NC}"
echo
echo "This means:"
echo "  ✓ ARI connection works"
echo "  ✓ Call origination works"
echo "  ✗ Trunk is rejecting the call"
echo
echo -e "${YELLOW}Possible Causes:${NC}"
echo

if echo "$VOIPMS_STATUS" | grep -q "online"; then
    echo -e "  ${GREEN}✓${NC} Trunk appears to be online"
    echo
    echo "  Since trunk is online, the issue is likely:"
    echo "    • Provider rejecting the call (balance, restrictions)"
    echo "    • Number format not accepted by provider"
    echo "    • Codec mismatch"
    echo "    • Call limit reached"
else
    echo -e "  ${RED}✗${NC} Trunk may not be properly registered"
    echo
    echo "  Check FreePBX trunk configuration:"
    echo "    1. FreePBX > Connectivity > Trunks"
    echo "    2. Verify registration status"
    echo "    3. Check credentials"
fi

echo
echo -e "${YELLOW}Next Steps:${NC}"
echo
echo "1. Check Asterisk logs in real-time:"
echo "   ${BLUE}On FreePBX server (10.0.10.6):${NC}"
echo "   tail -f /var/log/asterisk/full | grep -i 'voipms\\|19199129332\\|congestion'"
echo
echo "2. Try manual call from Asterisk CLI:"
echo "   ${BLUE}On FreePBX server:${NC}"
echo "   asterisk -rx \"channel originate PJSIP/19199129332@voipms application Playback demo-congrats\""
echo
echo "3. Check trunk registration:"
echo "   ${BLUE}On FreePBX server:${NC}"
echo "   asterisk -rx \"pjsip show endpoints\" | grep voipms"
echo "   asterisk -rx \"pjsip show registrations\""
echo
echo "4. Check provider account:"
echo "   • Balance sufficient"
echo "   • Outbound calling enabled"
echo "   • No call restrictions"
echo
echo -e "${BLUE}════════════════════════════════════════════════${NC}"
echo
echo "For detailed troubleshooting, see:"
echo "  TROUBLESHOOTING.md"
echo
