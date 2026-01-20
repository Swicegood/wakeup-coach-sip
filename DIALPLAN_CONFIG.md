# Dialplan Configuration for Wake-Up Coach

## Overview

The wakeup-coach app needs the **answered PJSIP trunk leg** to enter Stasis, not the Local channel. This requires a special dialplan that:
1. Dials out via the trunk
2. After answer, redirects the PJSIP channel to Stasis

## Required Dialplan

Add this to `/etc/asterisk/extensions_custom.conf`:

```ini
;; Wake-up Coach - Trigger context
;; This context originates the outbound call and uses G() to redirect
;; the answered PJSIP leg to Stasis
[wakeup-trigger]
exten => _X.,1,NoOp(Wake-up Coach: Dialing ${EXTEN})
 same => n,Dial(PJSIP/${EXTEN}@voipms,60,G(wakeup-answered^s^1))
 same => n,Hangup()

;; After-answer context - enters Stasis on the PJSIP leg
[wakeup-answered]
exten => s,1,NoOp(Call answered - determining channel type)
 same => n,GotoIf($["${CHANNEL(channeltype)}" = "PJSIP"]?pjsip:hangup)
 same => n(pjsip),NoOp(Entering Stasis on outbound PJSIP leg: ${CHANNEL})
 same => n,Stasis(wakeup-coach)
 same => n,Hangup()
 same => n(hangup),NoOp(Hanging up non-PJSIP leg: ${CHANNEL})
 same => n,Hangup()
```

**Replace `voipms` with your actual trunk name.**

## How It Works

1. App originates: `Local/<number>@wakeup-trigger`
2. Dialplan calls: `Dial(PJSIP/<number>@voipms,...,G(wakeup-answered^s^1))`
3. Phone rings, user answers
4. `G()` option redirects BOTH legs to `[wakeup-answered]` context
5. We check `CHANNEL(channeltype)`:
   - **PJSIP channel** → enters `Stasis(wakeup-coach)` → app receives `StasisStart`
   - **Local channel** → hangs up (we don't need it)
6. App handles the call on the real PJSIP channel with audio

## Apply Changes

```bash
asterisk -rx "dialplan reload"
asterisk -rx "dialplan show wakeup-trigger"
asterisk -rx "dialplan show wakeup-answered"
```

## Verification Checklist

Before testing calls:

1. **Dialplan loaded:**
   ```bash
   asterisk -rx "dialplan show wakeup-trigger"
   # Should show: Dial(PJSIP/${EXTEN}@voipms,60,G(...))
   ```

2. **App registered:**
   ```bash
   asterisk -rx "ari show apps"
   # Should show: wakeup-coach
   ```

3. **Trunk available:**
   ```bash
   asterisk -rx "pjsip show endpoints" | grep voipms
   # Check status is Available or has contact
   ```

## Testing

1. Start the app (WebSocket must be connected first!)
2. Wait for "ari show apps" to show `wakeup-coach`
3. Originate test call:
   ```bash
   asterisk -rx "channel originate Local/19199129332@wakeup-trigger application Wait 60"
   ```
4. Answer your phone
5. Check app logs for `StasisStart` event with PJSIP channel

## Troubleshooting

### "Stasis app 'wakeup-coach' not registered"
- App's WebSocket is not connected
- Check app is running before originating calls
- Verify with: `asterisk -rx "ari show apps"`

### CONGESTION / Circuit congestion
- Trunk not available or misconfigured
- Check: `asterisk -rx "pjsip show endpoints"`
- Verify trunk name in dialplan matches

### No StasisStart event
- Dialplan not reaching Stasis() application
- Check: `asterisk -rx "core set verbose 5"` then watch logs
- Verify G() option is executing

### App receives StasisStart but wrong channel
- Should be PJSIP channel, not Local
- Check channel name in event starts with "PJSIP/"
