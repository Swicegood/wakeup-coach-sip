# Current Implementation Status

## What's Working ✅

### Core Functionality
1. **Call Origination**: Successfully originates calls via Local channel through FreePBX dialplan
   - Routes through `wakeup-trigger` context with `Dial()` and `G()` option
   - PJSIP channel enters Stasis after answer
   - Local channel destruction is correctly ignored

2. **ARI Integration**: Full ARI WebSocket connection with auto-reconnect
   - Authentication working
   - Event handlers for StasisStart, ChannelDestroyed, ChannelStateChange
   - WebSocket auto-reconnects if connection drops

3. **OpenAI Realtime API**: Bidirectional audio streaming working
   - Fresh session created for each call
   - VAD (Voice Activity Detection) with tuned thresholds
   - Real-time transcription and response generation

4. **RTP Audio Bridge**: Full bidirectional audio via RTP relay
   - External media channel connects to RTP relay
   - Audio flows: Phone ↔ Asterisk ↔ RTP Relay ↔ App ↔ OpenAI
   - Proper sample rate handling (8kHz/16kHz/24kHz)

5. **Sleep Detection**: Working silence detection and callback
   - Detects 10 seconds of silence
   - Prompts "Are you sleeping?"
   - Waits 10 seconds for response
   - Hangs up and calls back if no response

6. **Callback Loop**: Reliable callback after hang-up
   - Calls back when user hangs up (assumes they fell asleep)
   - Only stops calling back when user says "goodbye" AND doorbell is activated
   - Race condition protection with `conversation_starting` flag
   - No duplicate calls on hang-up

7. **Doorbell Integration**: Webhook ready for doorbell trigger
   - HTTP endpoint for doorbell signal
   - Combined with "goodbye" keyword to end callbacks

## Recent Fixes (2026-01-20)

1. **Fixed duplicate calls on hang-up**: Added `conversation_starting` flag to prevent race conditions during conversation setup

2. **Fixed ARI WebSocket disconnection**: Added auto-reconnect loop so callbacks receive events

3. **Fixed Local channel handling**: Now correctly ignores Local channel destruction (only tracks PJSIP channels)

4. **Fixed callback loop stopping on errors**: Errors in conversation setup no longer stop the callback loop

## Architecture

```
┌─────────────┐     WebSocket      ┌──────────────┐
│   OpenAI    │ ←───────────────→  │  wakeup-coach │
│  Realtime   │     (audio)        │     app       │
└─────────────┘                    └──────┬───────┘
                                          │ RTP/UDP
                                          ↓
                                   ┌──────────────┐
                                   │  RTP Relay   │
                                   │ (10.0.10.91) │
                                   └──────┬───────┘
                                          │ RTP/UDP
                                          ↓
┌─────────────┐     PJSIP/SIP      ┌──────────────┐
│    Phone    │ ←───────────────→  │   Asterisk   │
│             │                    │   FreePBX    │
└─────────────┘                    └──────────────┘
```

## Configuration

### Environment Variables
- `PHONE_NUMBER`: Target phone number
- `WAKE_KEYWORD`: Word to end callbacks (default: "goodbye")
- `ASTERISK_ARI_*`: ARI connection settings
- `OPENAI_API_KEY`: OpenAI API key
- `EXTERNAL_RTP_HOST`: RTP relay IP
- `EXTERNAL_RTP_HOST_PORT`: RTP relay port

### Dialplan (extensions_custom.conf)
```ini
[wakeup-trigger]
exten => _X.,1,NoOp(Wake-up Coach: Dialing ${EXTEN})
 same => n,Dial(PJSIP/${EXTEN}@voipms,60,G(wakeup-answered^s^1))
 same => n,Hangup()

[wakeup-answered]
exten => s,1,NoOp(Call answered - determining channel type)
 same => n,GotoIf($["${CHANNEL(channeltype)}" = "PJSIP"]?pjsip:hangup)
 same => n(pjsip),NoOp(Entering Stasis on outbound PJSIP leg: ${CHANNEL})
 same => n,Stasis(wakeup-coach)
 same => n,Hangup()
 same => n(hangup),NoOp(Hanging up non-PJSIP leg: ${CHANNEL})
 same => n,Hangup()
```

## Test Command

```bash
docker-compose down && docker-compose build wakeup-coach && docker-compose up wakeup-coach
```

## Behavior Summary

1. App starts → connects to ARI WebSocket → connects to OpenAI
2. Originates call via `Local/number@wakeup-trigger`
3. Asterisk dials out via PJSIP trunk
4. On answer, PJSIP channel enters Stasis
5. App creates bridge + external media channel
6. OpenAI session started, initial greeting sent
7. Bidirectional conversation flows
8. If 10s silence → "Are you sleeping?" → 10s wait → hang up + callback
9. If user says "goodbye" + doorbell activated → end permanently
10. If user hangs up without goodbye → callback in 2 seconds
