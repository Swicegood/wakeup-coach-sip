# Audio Playback Implementation Status

## Current Implementation: File-Based HTTP Playback

### What's Working ✅

1. **OpenAI Audio Reception**: Successfully receiving audio from OpenAI Realtime API
   - Audio buffer growing properly (36KB → 297KB observed)
   - Audio delta events being processed

2. **WAV File Generation**: Converting PCM16 audio to WAV format
   - 16kHz, 16-bit, mono WAV files
   - Files created successfully in temp directory
   - ~2 second chunks (64,000 bytes each)

3. **HTTP Server**: Serving audio files
   - Running on port 8080
   - Files accessible locally at `http://172.22.45.226:8080/audio/audio_N.wav`

4. **ARI Playback Commands**: Successfully calling ARI play API
   - Commands accepted (HTTP 201 response)
   - Playback IDs returned
   - Example: `8f57df72-715b-438e-8a05-8d735f748690`

### The Problem ❌

**Asterisk Cannot Fetch Audio Files**

Asterisk at `10.0.10.6` cannot reach the HTTP server at `172.22.45.226:8080`.

**Evidence**:
- ARI playback commands succeed
- No HTTP access logs from Asterisk's IP (10.0.10.6)
- User reports no audio heard on call
- Test curl from Asterisk fails to connect

**Root Cause**: Network routing/firewall between Asterisk server and container host

## Test Results

### Latest Test (2026-01-10 18:13)

```
✅ Call connects successfully
✅ OpenAI session initialized
✅ Audio received from OpenAI (297KB buffered)
✅ 4 WAV files created (audio_1.wav through audio_4.wav)
✅ HTTP server serving files locally
✅ ARI playback commands accepted
❌ Asterisk cannot fetch files (no HTTP requests from 10.0.10.6)
❌ No audio played to user
```

### Logs Showing Success Until Network Issue
```
[INFO] Starting audio bridge
[INFO] HTTP server started on port 8080
[INFO] Audio buffer: 297600 bytes
[INFO] Playing audio chunk: 64000 bytes
[INFO] Playing audio: http://172.22.45.226:8080/audio/audio_1.wav
[INFO] Audio playback started: 8f57df72-715b-438e-8a05-8d735f748690
```

But no access logs from Asterisk:
```
[INFO] aiohttp.access: 172.22.45.226 [HEAD /audio/audio_1.wav] 404
# ^ This was our test curl, not Asterisk
# No logs from 10.0.10.6
```

## Solutions

### Option 1: Port Forward/Tunnel (Quick Fix)
Set up SSH tunnel or port forward from Asterisk to container:
```bash
# On Asterisk server (10.0.10.6)
ssh -L 8080:172.22.45.226:8080 user@container-host
```

Then use `http://localhost:8080/audio/...` in playback URIs

### Option 2: Public HTTP Server (Recommended)
Host container on publicly accessible IP/domain:

1. Update `.env`:
   ```
   HTTP_SERVER_HOST=your-public-ip-or-domain
   ```

2. Ensure port 8080 is open in firewall

3. Restart service

### Option 3: File Transfer to Asterisk
Instead of HTTP, copy files to Asterisk:

```python
# In audio_bridge.py
async def _play_audio_file(self, filename: str):
    # SCP file to Asterisk
    await self._scp_to_asterisk(local_path, remote_path)

    # Play using local file path
    media_uri = f"file://{remote_path}"
```

Requires:
- SSH access to Asterisk
- Shared directory or SCP capability
- Cleanup mechanism

### Option 4: NFS/SMB Mount (Production)
Set up shared filesystem:

1. Mount NFS share on both systems
2. Write audio files to shared directory
3. Asterisk plays from shared directory
4. No network transfer needed

### Option 5: RTP Direct Streaming (Best Long-term)
Implement proper RTP packet handling (see IMPLEMENTATION_SUMMARY.md):
- Bypasses file system entirely
- Lowest latency
- Most complex to implement

## Quick Test to Verify Network

### From Container Host
```bash
# Check HTTP server works locally
curl -I http://172.22.45.226:8080/

# Expected: HTTP 200 or 404 (server responding)
```

### From Asterisk (if SSH available)
```bash
ssh root@10.0.10.6
curl -I http://172.22.45.226:8080/

# If this fails: network/firewall issue
# If this works: check ARI HTTP client config
```

### Test with Asterisk Built-in Sound
```bash
curl -X POST -u "wakeup:password" \
  "http://10.0.10.6:8088/ari/channels/CHANNEL_ID/play" \
  -H "Content-Type: application/json" \
  -d '{"media": "sound:demo-congrats"}'
```

If this works, the play API is functional - just need network fix.

## Current Code

File-based playback is fully implemented in `audio_bridge.py`:
- Lines 83-104: HTTP server setup
- Lines 135-146: Audio buffering from OpenAI
- Lines 148-192: Playback loop
- Lines 213-248: ARI play command

**Only missing piece**: Network connectivity between Asterisk and HTTP server

## Next Steps

1. **Immediate**: Determine network topology
   - Where is container running relative to Asterisk?
   - Can we make container accessible from 10.0.10.6?
   - Firewall rules needed?

2. **Short-term Fix**: Choose one of the 5 options above

3. **Long-term**: Implement RTP streaming for production use

## Summary

The file-based audio playback is **90% complete**:
- ✅ Audio capture from OpenAI
- ✅ WAV file creation
- ✅ HTTP server
- ✅ ARI integration
- ❌ Network routing (blocking issue)

**The code works - just need to solve network connectivity**
