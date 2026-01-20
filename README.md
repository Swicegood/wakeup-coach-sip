# Wake-Up Coach

A dockerized wake-up coach service that integrates with Asterisk via ARI and uses OpenAI's audio-native realtime model to call you in the morning and converse until you are awake.

## Configuration Setup

### 1. Create Environment File

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

### 2. Configure Asterisk ARI

Edit `.env` and set your Asterisk ARI credentials:

```bash
ARI_HOST=localhost          # Asterisk server hostname/IP
ARI_PORT=8088              # ARI HTTP port (default: 8088)
ARI_USERNAME=asterisk      # ARI username
ARI_PASSWORD=asterisk      # ARI password
ARI_APP_NAME=wakeup-coach  # ARI application name
```

**Note:** You need to configure the ARI user in Asterisk's `/etc/asterisk/ari.conf`:

```ini
[asterisk]
type = user
read_only = no
password = asterisk
```

### 3. Configure OpenAI API

Set your OpenAI API key in `.env`:

```bash
OPENAI_API_KEY=sk-your-actual-api-key-here
OPENAI_MODEL=gpt-4o-realtime-preview-2024-12-17
```

Get your API key from: https://platform.openai.com/api-keys

### 4. Configure Call Settings

Set the target phone number and wake keyword:

```bash
TARGET_PHONE_NUMBER=+1234567890  # Your phone number
WAKE_KEYWORD=awake               # Keyword to detect wakefulness
```

### 5. Optional: Adjust Logging

```bash
LOG_LEVEL=INFO  # Options: DEBUG, INFO, WARNING, ERROR
```

## Security

- The `.env` file is automatically ignored by git to protect your credentials
- Never commit `.env` files or API keys to version control
- Keep your OpenAI API key secure and rotate it if exposed

## Running the Service

### Using Docker Compose (Recommended)

Build and run the service:

```bash
docker-compose up -d
```

View logs:

```bash
docker-compose logs -f
```

Stop the service:

```bash
docker-compose down
```

### Using Docker Directly

Build the image:

```bash
docker build -t wakeup-coach .
```

Run the container:

```bash
docker run --env-file .env --network host wakeup-coach
```

### Running Locally (Development)

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the service:

```bash
python main.py
```

## How It Works

1. **Call Initiation**: The service connects to Asterisk ARI and originates a call to your phone number
2. **Call Answer**: When you answer, the call enters the Stasis application
3. **Audio Streaming**: Audio is bridged between Asterisk and OpenAI Realtime API
4. **Conversation**: OpenAI's voice model converses with you to help you wake up
5. **Wake Detection**: When you say the wake keyword (default: "goodbye"), the call ends
6. **Graceful Shutdown**: All resources are cleaned up properly

## Architecture

```
┌─────────────┐         ┌──────────────┐         ┌──────────────┐
│  Asterisk   │◄───────►│  Wake-Up     │◄───────►│   OpenAI     │
│     ARI     │  HTTP   │   Coach      │   WSS   │  Realtime    │
│             │◄───────►│   Service    │◄───────►│     API      │
│  WebSocket  │   WSS   │              │         │              │
└─────────────┘         └──────────────┘         └──────────────┘
       │                                                 │
       │                                                 │
       ▼                                                 ▼
   Your Phone ◄──── Audio (both directions) ────► AI Voice Model
```

## Configuration API

The `config.py` module provides a typed configuration loader:

```python
from config import load_config

# Load configuration from environment
config = load_config()

# Access configuration
print(config.asterisk.base_url)
print(config.openai.api_key)
print(config.call.target_phone_number)
```

The loader will raise a `ValueError` with helpful error messages if required environment variables are missing.

## Important Notes

- **Network Mode**: Uses `host` network mode to access Asterisk on localhost
- **Single Call**: Designed for single-user, single-call operation
- **Audio Format**: Uses PCM16 (16kHz signed linear) for audio streaming
- **Wake Keyword**: Transcriptions are checked for the wake keyword to detect wakefulness
