# Troubleshooting Guide

## Viewing PJSIP Logs on the Server

To debug PJSIP issues, you need to view logs on your Asterisk server (typically at `10.0.10.6`).

### Quick Commands

```bash
# SSH into your Asterisk server
ssh root@10.0.10.6

# View real-time PJSIP logs (exclude noisy Manager API messages)
tail -f /var/log/asterisk/full | grep -i pjsip | grep -v "manager.c.*Login"

# View logs for a specific trunk (e.g., voipms)
tail -f /var/log/asterisk/full | grep -i "voipms\|pjsip" | grep -v "manager.c.*Login"

# View only PJSIP errors and important messages
tail -f /var/log/asterisk/full | grep -iE "pjsip.*(error|fail|reject|unavailable|unregistered)" | grep -v "manager.c.*Login"
```

### PJSIP Status Commands

Check PJSIP status directly via Asterisk CLI:

```bash
# Show all PJSIP endpoints
asterisk -rx "pjsip show endpoints"

# Show registration status
asterisk -rx "pjsip show registrations"

# Show contacts
asterisk -rx "pjsip show contacts"

# Show Address of Record (AOR) status
asterisk -rx "pjsip show aors"

# Show details for a specific endpoint
asterisk -rx "pjsip show endpoint voipms"

# Filter for your trunk
asterisk -rx "pjsip show endpoints" | grep voipms
asterisk -rx "pjsip show registrations" | grep voipms
```

### Real-Time Log Monitoring

Monitor logs while making a call:

```bash
# View all Asterisk logs in real-time (very verbose)
tail -f /var/log/asterisk/full

# Filter for PJSIP-related messages (exclude Manager API noise)
tail -f /var/log/asterisk/full | grep -i pjsip | grep -v "manager.c.*Login"

# Filter for your trunk and phone number
tail -f /var/log/asterisk/full | grep -i "voipms\|+19199129332\|pjsip" | grep -v "manager.c.*Login"

# View with context (5 lines before/after matches)
tail -f /var/log/asterisk/full | grep -i -A 5 -B 5 pjsip | grep -v "manager.c.*Login"

# Focus on registration and endpoint status
tail -f /var/log/asterisk/full | grep -iE "pjsip.*(register|endpoint|aor|contact|qualify)" | grep -v "manager.c.*Login"

# Focus on call-related PJSIP messages
tail -f /var/log/asterisk/full | grep -iE "pjsip.*(invite|call|channel|originate)" | grep -v "manager.c.*Login"
```

### View Recent Logs

```bash
# Last 100 lines filtered for PJSIP
tail -100 /var/log/asterisk/full | grep -i pjsip

# Last 50 lines for your trunk
tail -50 /var/log/asterisk/full | grep -i "voipms\|congestion\|failed\|reject"
```

### Interactive Asterisk CLI

For more detailed debugging, connect to Asterisk CLI interactively:

```bash
# Connect to Asterisk CLI with verbose output
asterisk -rvvv

# Once connected, you can run commands:
pjsip show endpoints
pjsip show registrations
pjsip show endpoint voipms
core set verbose 5
core set debug 5
```

### Enable Verbose PJSIP Logging

If you need more detailed logs, enable verbose logging:

```bash
# In Asterisk CLI (asterisk -rvvv)
pjsip set logger on
core set verbose 5
core set debug 5

# Or edit /etc/asterisk/logger.conf to increase verbosity
# Then reload: asterisk -rx "module reload logger"
```

### Common Log Locations

- **Main log file**: `/var/log/asterisk/full`
- **Error log**: `/var/log/asterisk/messages`
- **Queue log**: `/var/log/asterisk/queue_log`
- **CDR log**: `/var/log/asterisk/cdr-csv/Master.csv`

### Understanding PJSIP Logs

Common log patterns and what they mean:

**Manager API Login Messages (Noisy - can be filtered):**
```
DEBUG[5333]: manager.c:6683 process_message: Running action 'Login'
```
- These appear every 3 seconds from monitoring tools
- Safe to filter out: `grep -v "manager.c.*Login"`

**PJSIP OPTIONS Requests (Endpoint Qualification):**
```
DEBUG[2058]: res_pjsip/pjsip_options.c:927 sip_options_qualify_aor: Qualifying all contacts on AOR '972'
```
- Asterisk checking if endpoints are reachable
- Normal health-check activity
- Shows RTT (Round Trip Time) for each endpoint

**PJSIP REGISTER Requests:**
```
DEBUG[934]: res_pjsip/pjsip_distributor.c:394 find_dialog: Could not find matching transaction for Request msg REGISTER
```
- Endpoints registering with Asterisk
- Shows successful registrations: `Refreshed contact 'sip:216@10.0.10.25:5081'`

**Endpoint Identification:**
```
DEBUG[2058]: res_pjsip_endpoint_identifier_ip.c:260 ip_identify_match_check: Source address 10.0.10.25:5081 does not match identify 'voipms'
```
- Asterisk trying to identify which endpoint a request belongs to
- If voipms doesn't match, check your identify configuration

**WebSocket Activity:**
```
DEBUG[4962]: res_http_websocket.c:382 __ast_websocket_write: Writing websocket pong frame
```
- ARI WebSocket keepalive (normal)

**Call Origination (INVITE):**
```
VERBOSE[2152] res_pjsip_logger.c: <--- Transmitting SIP request (1043 bytes) to UDP:208.100.60.17:5060 --->
INVITE sip:19199129332@atlanta.voip.ms:5060 SIP/2.0
```
- Call is being sent to provider
- Check the response that follows:
  - **200 OK** = Call accepted ✅
  - **401 Unauthorized** = Authentication failed ❌ (see troubleshooting section)
  - **403 Forbidden** = Provider rejected call ❌
  - **404 Not Found** = Number not found ❌
  - **486 Busy Here** = Destination busy ❌
  - **503 Service Unavailable** = Provider issue ❌

**RTP Media Setup:**
```
DEBUG[2058] res_rtp_asterisk.c: Allocated port 19482 for RTP instance
```
- RTP port allocated for audio
- Normal part of call setup

### Quick Diagnostic Script

Run this comprehensive check:

```bash
echo "=== Trunk Status ==="
asterisk -rx "pjsip show endpoints" | grep voipms
echo ""
echo "=== Registration Status ==="
asterisk -rx "pjsip show registrations" | grep voipms
echo ""
echo "=== Recent PJSIP Errors (filtered) ==="
tail -50 /var/log/asterisk/full | grep -i "voipms\|pjsip\|congestion\|failed\|reject" | grep -v "manager.c.*Login"
```

## Error: HTTP 401 - Authentication Failed

If you see this error in the logs:
```
Failed to connect to ARI WebSocket: server rejected WebSocket connection: HTTP 401
```

**Problem**: The ARI password in your `.env` file is incorrect or is a hashed password instead of plaintext.

### Solution: Get the Correct ARI Password

#### Option 1: Check FreePBX Web Interface

1. Log into FreePBX web interface: `http://10.0.10.6/admin`
2. Go to **Settings** → **Asterisk REST Interface (ARI Users)**
3. Find the `freepbxuser` entry
4. The password shown there is the **plaintext** password you need
5. Copy it to your `.env` file

#### Option 2: Access Asterisk Server Directly

If you have SSH access to the Asterisk server:

```bash
# SSH into the FreePBX server
ssh root@10.0.10.6

# Check the ARI configuration
cat /etc/asterisk/ari.conf
```

Look for the `[freepbxuser]` section. The password line will have the hashed password, but you need to know what plaintext password generates that hash.

#### Option 3: Reset the ARI Password

If you can't find the password, reset it:

1. SSH into FreePBX server
2. Edit `/etc/asterisk/ari.conf`:
   ```bash
   nano /etc/asterisk/ari.conf
   ```

3. Find the `[freepbxuser]` section and change the password:
   ```ini
   [freepbxuser]
   type = user
   read_only = no
   password = YOUR_NEW_PASSWORD_HERE
   ```

4. Reload Asterisk:
   ```bash
   asterisk -rx "module reload res_ari.so"
   ```

5. Update your `.env` file with the new password:
   ```bash
   ARI_PASSWORD=YOUR_NEW_PASSWORD_HERE
   ```

#### Option 4: Create a New ARI User

Create a dedicated user for the wake-up coach:

1. SSH into FreePBX
2. Edit `/etc/asterisk/ari.conf`:
   ```bash
   nano /etc/asterisk/ari.conf
   ```

3. Add a new user section:
   ```ini
   [wakeupcoach]
   type = user
   read_only = no
   password = your_secure_password_here
   ```

4. Reload Asterisk:
   ```bash
   asterisk -rx "module reload res_ari.so"
   ```

5. Update your `.env` file:
   ```bash
   ARI_USERNAME=wakeupcoach
   ARI_PASSWORD=your_secure_password_here
   ```

### Testing Your Credentials

Test your ARI credentials with curl:

```bash
curl -u "USERNAME:PASSWORD" http://10.0.10.6:8088/ari/asterisk/info
```

If successful, you'll see JSON output with Asterisk info. If you see `{"message":"Authentication required"}`, the credentials are wrong.

### Common Mistakes

1. **Using the hashed password**: The password in `ari.conf` is hashed (starts with `$6$`), but you need the **plaintext** password in `.env`
2. **Wrong username**: Make sure the username matches exactly (case-sensitive)
3. **ARI not enabled**: Ensure ARI is enabled in `/etc/asterisk/ari.conf` with `enabled = yes`
4. **HTTP not enabled**: Check `/etc/asterisk/http.conf` has `enabled=yes`

## Other Common Issues

### Container Keeps Restarting

Check logs:
```bash
docker-compose logs -f
```

### Can't Connect to Asterisk

1. Verify Asterisk is running:
   ```bash
   # On the Asterisk server
   systemctl status asterisk
   ```

2. Check network connectivity:
   ```bash
   ping 10.0.10.6
   curl http://10.0.10.6:8088/ari/asterisk/info
   ```

3. Verify firewall allows port 8088

### OpenAI Connection Failed

1. Verify your API key is correct
2. Check you have Realtime API access
3. Test connectivity:
   ```bash
   curl -H "Authorization: Bearer YOUR_API_KEY" https://api.openai.com/v1/models
   ```

## Error: Call Originates but Never Enters Stasis (No StasisStart Event)

**Problem**: The call is being originated successfully, but the ARI application never receives a `StasisStart` event. This typically happens when code changes (especially adding "hang up and call back" logic) break the originate flow, OR when Asterisk/FreePBX configuration routes the channel through dialplan before Stasis.

### Symptoms

- Call originates successfully (you see it in logs)
- ARI response shows channel was created
- But `StasisStart` event is never received
- Your application code never executes
- Logs show the call going to `from-pstn` or `from-internal` contexts instead of Stasis
- Stasis topics are created but immediately destroyed (no StasisStart event in between)
- ARI channel response shows `"dialplan": {"context": "from-pstn", ...}` instead of being in Stasis

### Root Cause

When using ARI originate with the `app` parameter, the channel **should** enter Stasis immediately. However, if the channel is routed through dialplan (like `from-pstn` or `from-internal`), it means:

1. The `app` parameter is being ignored/bypassed
2. OR FreePBX/Asterisk configuration is intercepting the channel before Stasis
3. OR the endpoint format causes dialplan routing to happen first

**Evidence**: Stasis logs show topics created then immediately destroyed without StasisStart:
```
DEBUG: stasis.c: Creating topic. name: channel:1768161160.4
DEBUG: stasis.c: Destroying topic. name: channel:1768161160.4
```
(No StasisStart event in between = channel never entered Stasis)

### Common Causes After Adding Call-Back Logic

#### 1. Originate No Longer Sets Stasis App Correctly

**Check**: Verify `originate_call` includes the `app` parameter:

```python
# In ari_client.py - CORRECT:
payload = {
    "endpoint": endpoint,
    "app": self.config.app_name,  # ✅ Must be present
    "appArgs": "dialed",
    "callerId": caller_id,
}

# WRONG (missing app):
payload = {
    "endpoint": endpoint,
    "callerId": caller_id,  # ❌ Missing app parameter
}
```

**Fix**: Ensure every `POST /ari/channels` request includes `app=wakeup-coach` (or your app name).

#### 2. App Name Mismatch

**Check**: Verify the app name matches exactly (case-sensitive) in:

1. Your WebSocket subscription URL:
   ```python
   # In ari_client.py
   f"?app={self.config.app_name}&api_key=..."
   ```

2. Every originate call:
   ```python
   payload = {"app": self.config.app_name, ...}
   ```

3. Your `.env` file:
   ```bash
   ARI_APP_NAME=wakeup-coach  # Must match exactly
   ```

**Common mismatches**:
- `wakeup-coach` vs `wakeup_coach` vs `wakeupcoach`
- Case differences: `WakeUp-Coach` vs `wakeup-coach`

**Fix**: Use the same exact string everywhere. Default is `wakeup-coach`.

#### 3. Originate Uses Channel Type/Context That Never Hits Stasis

**Problem**: Changed endpoint format that bypasses Stasis.

**Current code** (should work):
```python
# Uses Local channel to route through dialplan
endpoint = f"Local/{dial_number}@from-internal"
```

**Problematic changes** (won't hit Stasis):
```python
# ❌ Direct PJSIP - bypasses dialplan/Stasis
endpoint = f"PJSIP/{number}@voipms"

# ❌ Wrong context
endpoint = f"Local/{number}@from-pstn"  # Wrong context
```

**Check**: Look at your logs:
```
DestContext: from-pstn  # ❌ Wrong - should go through Stasis first
```

vs

```
StasisStart event received  # ✅ Correct
```

**Fix**: 
- **Both `PJSIP/{number}@voipms` AND `Local/{number}@from-internal` route through dialplan instead of entering Stasis**
- This indicates a deeper configuration issue - the `app` parameter in ARI originate isn't working as expected
- **Possible solutions**:
  1. **Check if WebSocket subscription is active** - StasisStart only works if you're subscribed to the app via WebSocket
  2. **Verify app name matches exactly** between originate and WebSocket subscription
  3. **Check Asterisk version compatibility** - Some versions/configurations may route through dialplan first
  4. **Alternative**: Use dialplan `Stasis()` application - create dialplan extension that routes to `Stasis(wakeup-coach)` before outbound routes
  5. **Check for FreePBX modules interfering** - Some FreePBX modules may intercept channels before Stasis

#### 4. Race Condition: Hangup Before StasisStart

**Problem**: Call-back logic hangs up the channel before it enters Stasis.

**Check**: Add logging around originate and StasisStart:

```python
async def _originate_call(self):
    self.logger.info(f"[{datetime.now()}] Originating call...")
    channel = await self.ari.originate_call(...)
    self.logger.info(f"[{datetime.now()}] Call originated: {channel['id']}")

async def _handle_stasis_start(self, event: dict):
    self.logger.info(f"[{datetime.now()}] StasisStart received: {event['channel']['id']}")
```

**Fix**: 
- Don't hangup until you've seen `StasisStart`
- Add timeout logic (e.g., wait 60s for StasisStart before assuming failure)
- Ensure call-back loop doesn't interrupt active Stasis sessions

### Debugging Steps

1. **Check if StasisStart is received**:
   ```python
   # Add to _handle_stasis_start
   self.logger.info(f"✅ StasisStart received for {channel_id}")
   ```

2. **Verify app name in logs**:
   ```bash
   # Check WebSocket subscription
   grep "app=" /var/log/your-app.log
   
   # Check originate calls
   grep "Originating call" /var/log/your-app.log
   ```

3. **Check Asterisk logs for Stasis activity**:
   ```bash
   tail -f /var/log/asterisk/full | grep -i stasis
   ```

4. **Test with simple originate**:
   ```bash
   curl -u "USER:PASS" http://10.0.10.6:8088/ari/channels \
     -d '{"endpoint":"Local/123@from-internal","app":"wakeup-coach"}'
   ```

5. **Verify dialplan includes Stasis**:
   ```bash
   asterisk -rx "dialplan show from-internal" | grep -i stasis
   ```

### Quick Fix Checklist

- [ ] `app` parameter present in all originate calls
- [ ] App name matches exactly everywhere (no typos/case differences)
- [ ] Endpoint format routes through dialplan that includes Stasis
- [ ] No race conditions (don't hangup before StasisStart)
- [ ] WebSocket subscription uses same app name
- [ ] `.env` ARI_APP_NAME matches code

## Error: SIP 401 Unauthorized from VoIP Provider

If you see this in your PJSIP logs:

```
[2026-01-08 20:28:01] VERBOSE[2152] res_pjsip_logger.c: <--- Transmitting SIP request (1043 bytes) to UDP:208.100.60.17:5060 --->
INVITE sip:19199129332@atlanta.voip.ms:5060 SIP/2.0
...
[2026-01-08 20:28:01] VERBOSE[934] res_pjsip_logger.c: <--- Received SIP response (602 bytes) from UDP:208.100.60.17:5060 --->
SIP/2.0 401 Unauthorized
WWW-Authenticate: Digest algorithm=MD5, realm="atlanta1.voip.ms", nonce="5c9da5ce"
```

**Problem**: Your voipms trunk is sending INVITE requests without authentication credentials. VoIP.ms requires authentication for outbound calls.

### What's Happening

1. ✅ Call originates successfully from your application
2. ✅ PJSIP session is created and RTP port allocated
3. ✅ DNS resolution works (atlanta.voip.ms → 208.100.60.17)
4. ✅ INVITE request is sent to VoIP.ms
5. ❌ **VoIP.ms responds with 401 Unauthorized** - missing authentication

### Solution: Configure Trunk Authentication

Your voipms trunk needs to authenticate with VoIP.ms. Check your trunk configuration:

#### Step 1: Check Trunk Configuration in FreePBX

1. Log into FreePBX: `http://10.0.10.6/admin`
2. Go to **Connectivity** → **Trunks**
3. Find and edit your `voipms` trunk
4. Verify:
   - **Outbound CallerID** is set
   - **Registration** is configured (if using registration)
   - **Authentication** credentials are correct:
     - Username (usually your VoIP.ms account number)
     - Password (your VoIP.ms account password)
   - **Trunk is enabled**

#### Step 2: Check PJSIP Configuration

SSH into your Asterisk server and check the PJSIP configuration:

```bash
# On FreePBX server
asterisk -rx "pjsip show endpoint voipms"
asterisk -rx "pjsip show aor voipms"
asterisk -rx "pjsip show auth voipms"
```

Look for:
- **AOR (Address of Record)**: Should have `auth=voipms` or similar
- **Auth**: Should exist and have correct username/password
- **Endpoint**: Should reference the auth object

#### Step 3: Verify Authentication Object

The endpoint should have an `auth` property pointing to an authentication object:

```bash
# Check if auth object exists
asterisk -rx "pjsip show auth" | grep voipms

# Check endpoint configuration
asterisk -rx "pjsip show endpoint voipms" | grep -i auth
```

#### Step 4: Common Issues

**Issue 1: Missing Auth Object**
- The endpoint exists but has no `auth` property
- **Fix**: In FreePBX, edit the trunk and ensure authentication is configured

**Issue 2: Wrong Credentials**
- Username or password is incorrect
- **Fix**: Verify credentials in FreePBX trunk settings match your VoIP.ms account

**Issue 3: Auth Object Not Linked**
- Auth object exists but endpoint doesn't reference it
- **Fix**: In FreePBX trunk settings, ensure the authentication section is properly configured

**Issue 4: Registration vs. IP Authentication**
- VoIP.ms supports both registration-based and IP-based authentication
- If using IP-based, ensure your public IP (76.182.59.105) is whitelisted in VoIP.ms portal
- If using registration, ensure the trunk is registered: `asterisk -rx "pjsip show registrations" | grep voipms`

#### Step 5: Test After Fix

After fixing the authentication:

1. Reload PJSIP:
   ```bash
   asterisk -rx "pjsip reload"
   ```

2. Check registration (if using registration):
   ```bash
   asterisk -rx "pjsip show registrations" | grep voipms
   ```

3. Try the call again and watch logs:
   ```bash
   tail -f /var/log/asterisk/full | grep -i "voipms\|401\|invite" | grep -v "manager.c.*Login"
   ```

4. You should see the INVITE retry with authentication, or a 200 OK response instead of 401

### VoIP.ms Specific Notes

- VoIP.ms requires authentication for all outbound calls
- You can use either:
  - **Registration**: Trunk registers with VoIP.ms (recommended)
  - **IP Authentication**: Whitelist your public IP in VoIP.ms portal
- Check your VoIP.ms portal: https://www.voip.ms
  - Go to **Account** → **Sub Accounts** or **Main Account**
  - Verify credentials match
  - Check if IP authentication is enabled and your IP is whitelisted

## Error: Circuit/channel congestion

If you see this error in the logs:
```
Channel destroyed: Circuit/channel congestion
```

**Problem**: The call originates successfully through ARI but immediately fails at the trunk/provider level. This is a telephony configuration issue, not application code.

### Possible Causes

1. **Trunk Registration Issue**
   - The voipms (or other) trunk may not be properly registered
   - Trunk registration expired or failed

2. **Provider Rejection**
   - VoIP.ms (or your provider) may be rejecting the call
   - Account balance insufficient
   - Call limits reached
   - Outbound calling disabled or restricted
   - Number format not accepted by provider

3. **Number Format Issue**
   - The number format `+19199129332` might need adjustment
   - Provider may require different formatting (with/without +, country code, etc.)

4. **Codec Mismatch**
   - Audio codec mismatch between Asterisk and provider
   - Codec not supported by trunk

5. **Concurrent Call Limit**
   - Account may have reached concurrent call limit
   - Other active calls preventing new call

6. **Trunk Configuration**
   - Trunk not properly configured in FreePBX
   - Authentication credentials incorrect
   - Outbound routes not configured correctly

### Troubleshooting Steps

#### Step 1: Check Trunk Status in FreePBX

SSH into your FreePBX server and check trunk registration:

```bash
# On FreePBX server (10.0.10.6)
asterisk -rx "pjsip show endpoints"
asterisk -rx "pjsip show registrations"
```

Look for your trunk (e.g., `voipms`):
- Status should show `Unavailable/Unregistered` or `Avail`
- If showing `Unavailable/Unregistered`, the trunk is not registered

#### Step 2: Check Trunk Registration in FreePBX Web Interface

1. Log into FreePBX: `http://10.0.10.6/admin`
2. Go to **Connectivity** → **Trunks**
3. Find your trunk (e.g., `voipms`)
4. Check registration status
5. Verify outbound routes are configured and include this trunk

#### Step 3: Test Manual Call from Asterisk CLI

Test if the call works directly from Asterisk (bypasses our application):

```bash
# On FreePBX server
asterisk -rx "channel originate PJSIP/+19199129332@voipms application Playback demo-congrats"
```

- If this fails with the same error, the issue is with Asterisk/trunk configuration
- If this succeeds, the issue may be with the endpoint format in our application

#### Step 4: Check Asterisk Logs in Real-Time

Monitor Asterisk logs while attempting a call:

```bash
# On FreePBX server
tail -f /var/log/asterisk/full | grep -i "voipms\|congestion\|+19199129332"
```

Look for error messages that explain why the call failed.

#### Step 5: Try Different Number Formats

Edit your `.env` file and try different number formats:

```bash
# Try without + prefix
TARGET_PHONE_NUMBER=19199129332

# Or try with country code only
TARGET_PHONE_NUMBER=9199129332

# Or try as listed in your trunk
TARGET_PHONE_NUMBER=+1 919 912 9332
```

Then restart:
```bash
docker-compose restart
```

#### Step 6: Check Provider Account Status

If using VoIP.ms or another provider:

1. Log into your provider portal
2. Check account balance
3. Verify outbound calling is enabled
4. Check for any call restrictions or blocks
5. Verify the destination number is allowed
6. Check if there are concurrent call limits

#### Step 7: Verify Trunk Configuration in FreePBX

1. **Check PJSIP Settings**:
   - Go to **Settings** → **SIP Settings** → **PJSIP Settings**
   - Verify PJSIP is enabled and running

2. **Check Trunk Configuration**:
   - Go to **Connectivity** → **Trunks**
   - Edit your trunk
   - Verify:
     - Registration string is correct
     - Outbound CallerID is set
     - Codecs are configured correctly
     - Trunk is set to "Enabled"

3. **Check Outbound Routes**:
   - Go to **Connectivity** → **Outbound Routes**
   - Verify there's a route that:
     - Matches your number pattern (e.g., `+19199129332` or `19199129332`)
     - Uses the correct trunk (`voipms`)
     - Has appropriate priority

#### Step 8: Check for Stasis Application Configuration

Since we're using ARI/Stasis, verify the dialplan includes the Stasis application:

```bash
# On FreePBX server
asterisk -rx "dialplan show" | grep -i stasis
```

If nothing appears, you may need to create a dialplan entry that routes calls through the Stasis application. However, with ARI originate, this shouldn't be necessary unless the call needs to be intercepted after origination.

#### Step 9: Test with Different Endpoint Format

If your trunk name or endpoint format is different, update `call_manager.py`:

The current format is:
```python
endpoint = f"PJSIP/{self.config.call.target_phone_number}@voipms"
```

Try alternative formats:
```python
# Without trunk name (if using default route)
endpoint = f"PJSIP/{self.config.call.target_phone_number}"

# With different trunk name
endpoint = f"PJSIP/{self.config.call.target_phone_number}@your-trunk-name"
```

#### Step 10: Check Network and Firewall

Ensure network connectivity between Asterisk and provider:

```bash
# Test connectivity to provider
ping your-provider-server.com

# Check if firewall is blocking
iptables -L -n | grep 5060  # SIP port
iptables -L -n | grep 10000:20000  # RTP ports
```

### Quick Diagnostic Command

Run this comprehensive check on your FreePBX server:

```bash
echo "=== Trunk Status ==="
asterisk -rx "pjsip show endpoints" | grep voipms
echo ""
echo "=== Registration Status ==="
asterisk -rx "pjsip show registrations" | grep voipms
echo ""
echo "=== Recent Errors ==="
tail -50 /var/log/asterisk/full | grep -i "voipms\|congestion\|failed\|reject"
```

### Common Solutions

**If trunk is not registered:**
1. Check trunk credentials in FreePBX web interface
2. Verify registration string format
3. Check provider server status
4. Restart trunk registration: `asterisk -rx "pjsip reload"`

**If provider is rejecting calls:**
1. Add credit to your account
2. Enable outbound calling in provider portal
3. Remove any call restrictions
4. Verify destination number is allowed

**If number format is wrong:**
1. Try different formats (with/without +, spaces, dashes)
2. Check provider documentation for required format
3. Look at working calls in CDR to see what format was used

### Call Doesn't Originate

1. Check PJSIP endpoint exists in Asterisk
2. Verify trunk is registered
3. Check Asterisk logs: `tail -f /var/log/asterisk/full`

### No Audio

See NOTES.md - the audio playback path needs implementation.
