# Implementation Notes

## Current Status

The core Wake-Up Coach service has been implemented with the following components:

### Completed Components

1. **Configuration System** (`config.py`)
   - Type-safe configuration loading from environment variables
   - Validates required credentials
   - Supports Asterisk ARI and OpenAI API configuration

2. **ARI Client** (`ari_client.py`)
   - WebSocket connection for ARI events
   - Call origination and control
   - Channel management (answer, hangup)
   - Event handler registration system

3. **OpenAI Realtime Client** (`openai_client.py`)
   - WebSocket connection to OpenAI Realtime API
   - Audio streaming to OpenAI
   - Transcription monitoring
   - Wake keyword detection

4. **Audio Bridge** (`audio_bridge.py`)
   - Connects Asterisk channels to OpenAI
   - Audio streaming from Asterisk to OpenAI (via snoop)
   - Framework for bidirectional audio

5. **Call Manager** (`call_manager.py`)
   - Orchestrates the entire call lifecycle
   - Event handling and state management
   - Resource cleanup

6. **Main Application** (`main.py`)
   - Entry point with proper error handling
   - Logging configuration
   - Graceful shutdown

7. **Docker Support**
   - Dockerfile for containerization
   - docker-compose.yml for easy deployment

## Known Gaps & Next Steps

### Critical: Audio Playback Path

**Issue**: The audio path from OpenAI back to Asterisk is not fully implemented.

**Current State**:
- Audio FROM Asterisk TO OpenAI works (via channel snoop)
- Audio FROM OpenAI TO Asterisk is stubbed out (see `audio_bridge.py:_send_audio_to_asterisk`)

**Solution Options**:

1. **External Media Channel** (Recommended)
   - Use ARI's external media channel feature
   - Create a UDP/RTP server to receive audio from OpenAI
   - Stream to external media channel
   - Reference: `ari_client.py:create_external_media()`

2. **Media Playback API**
   - Buffer OpenAI audio chunks
   - Save as temporary audio files
   - Use ARI's play API
   - Higher latency, not ideal for realtime

3. **Asterisk Custom Module**
   - Write a custom Asterisk module
   - Directly inject audio into channel
   - Most complex but lowest latency

### PJSIP Endpoint Configuration

The service assumes a PJSIP endpoint is configured in Asterisk. You need:

```ini
; /etc/asterisk/pjsip.conf

[your-provider]
type=endpoint
context=from-provider
disallow=all
allow=ulaw
allow=alaw

[your-provider]
type=identify
endpoint=your-provider
match=provider.ip.address
```

Adjust `call_manager.py:_originate_call()` if using different endpoint format.

### Audio Format Considerations

- Currently using PCM16 (16kHz signed linear)
- ARI snoop defaults to slin16
- OpenAI Realtime API expects PCM16
- Format should match, but verify in testing

### Wake Keyword Detection

- Currently relies on OpenAI's transcription
- Transcription is asynchronous
- May have latency between speaking and detection
- Consider adding local VAD for faster response

### Error Recovery

Current implementation has basic error handling. Consider adding:
- Automatic reconnection for WebSocket failures
- Retry logic for ARI operations
- Fallback behaviors for network issues

### Testing Checklist

Before first production use:

- [ ] Verify Asterisk ARI is accessible
- [ ] Test ARI credentials and permissions
- [ ] Confirm PJSIP endpoint configuration
- [ ] Validate OpenAI API key
- [ ] Test call origination
- [ ] Verify audio streaming (both directions)
- [ ] Test wake keyword detection
- [ ] Verify graceful call termination
- [ ] Check log output at INFO level
- [ ] Test error scenarios (network down, wrong credentials, etc.)

## Development Tips

### Testing Locally

```bash
# Run without Docker for easier debugging
python main.py

# Watch logs in detail
LOG_LEVEL=DEBUG python main.py
```

### Inspecting ARI Events

Set `LOG_LEVEL=DEBUG` to see all ARI events in detail.

### OpenAI API Costs

The Realtime API charges for:
- Audio input tokens
- Audio output tokens
- Text tokens

Monitor usage at: https://platform.openai.com/usage

### Asterisk Configuration

Ensure your `/etc/asterisk/ari.conf` has:

```ini
[general]
enabled = yes
pretty = yes

[freepbxuser]
type = user
read_only = no
password = your-password-here
```

And `/etc/asterisk/http.conf`:

```ini
[general]
enabled=yes
bindaddr=0.0.0.0
bindport=8088
```

## Architecture Decisions

### Why WebSockets?

- ARI provides WebSocket for event streaming (essential)
- OpenAI Realtime API uses WebSocket (required)
- Low latency for audio streaming

### Why Not SIP/RTP Directly?

- Asterisk handles all telephony complexity
- ARI provides clean abstraction
- Follows the "non-negotiable" constraints in AGENT_SPEC.md

### Why Snoop Instead of Recording?

- Snoop provides realtime audio access
- Recording introduces latency
- Snoop allows bidirectional audio manipulation

### Why Python?

- Excellent async/await support
- Good WebSocket libraries
- OpenAI SDK available
- Simple deployment

## Future Enhancements (Out of Scope for MVP)

- Scheduled wake-up calls (cron integration)
- Multiple wake-up profiles
- Web UI for configuration
- Wake-up success tracking
- Gradual escalation (call again if not awake)
- Integration with calendar/alarm apps
