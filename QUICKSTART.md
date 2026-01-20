# Quick Start Guide

Get your Wake-Up Coach running in 5 minutes.

## Prerequisites

- Docker and Docker Compose installed
- Asterisk with ARI enabled
- OpenAI API key with Realtime API access
- PJSIP trunk configured in Asterisk

## Step 1: Configure Credentials

```bash
cp .env.example .env
```

Edit `.env` with your actual values:

```bash
# Your Asterisk ARI settings
ARI_HOST=10.0.10.6
ARI_USERNAME=freepbxuser
ARI_PASSWORD=your-actual-ari-password

# Your OpenAI API key
OPENAI_API_KEY=sk-your-actual-openai-key-here

# Your phone number (E.164 format)
TARGET_PHONE_NUMBER=+19199129332

# Wake keyword (what you'll say to end the call)
WAKE_KEYWORD=goodbye
```

## Step 2: Build and Run

```bash
docker-compose up -d
```

## Step 3: Watch the Logs

```bash
docker-compose logs -f
```

You should see:
```
Wake-Up Coach Service Starting
============================================================
Target phone: +19199129332
Wake keyword: goodbye
Asterisk ARI: http://10.0.10.6:8088/ari
OpenAI Model: gpt-realtime-mini
============================================================
Connecting to ARI WebSocket...
Connected to ARI WebSocket
Connecting to OpenAI Realtime API...
Connected to OpenAI Realtime API
Originating call to PJSIP/+19199129332
```

## Step 4: Answer Your Phone

The service will call you. Answer and start talking with the AI wake-up coach!

## Step 5: Say the Wake Keyword

When you're awake, say your wake keyword (default: "goodbye") and the call will end.

## Troubleshooting

For detailed troubleshooting, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md)

### "Failed to connect to ARI WebSocket"

- Check `ARI_HOST` and `ARI_PORT` are correct
- Verify Asterisk ARI is enabled: `systemctl status asterisk`
- Check ARI credentials in `/etc/asterisk/ari.conf`
- **Common Issue**: ARI password must be plaintext, not hashed (see TROUBLESHOOTING.md)

### "Circuit/channel congestion" (Call Originates but Fails)

The call originates successfully but immediately fails. This is a telephony configuration issue:

**Quick Checks:**
1. Verify trunk is registered: `asterisk -rx "pjsip show registrations"` (on FreePBX server)
2. Test manual call: `asterisk -rx "channel originate PJSIP/+19199129332@voipms application Playback demo-congrats"`
3. Check provider account (balance, call limits, restrictions)
4. Try different number format in `.env` (with/without + prefix)

**Detailed troubleshooting**: See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#error-circuitchannel-congestion)

### "Failed to originate call"

- Verify PJSIP endpoint exists in Asterisk
- Check trunk registration: `asterisk -rx "pjsip show endpoints"`
- Review Asterisk logs: `tail -f /var/log/asterisk/full`

### "Failed to connect to OpenAI"

- Verify API key is correct
- Check you have Realtime API access
- Ensure internet connectivity from container

### Call connects but no audio

- Check audio bridge implementation (see NOTES.md)
- Verify audio format compatibility
- Review both ARI and OpenAI logs

### Wake keyword not detected

- Speak clearly and wait a moment
- Try different wake keywords
- Check transcription logs (set `LOG_LEVEL=DEBUG`)

## Next Steps

1. Read [NOTES.md](NOTES.md) for implementation details and known gaps
2. Review [README.md](README.md) for full documentation
3. Implement the audio playback path (see NOTES.md)
4. Test thoroughly before relying on it for real wake-ups!

## Stopping the Service

```bash
docker-compose down
```

## Development Mode

Run locally without Docker for easier debugging:

```bash
pip install -r requirements.txt
python main.py
```
