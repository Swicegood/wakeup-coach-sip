"""Configuration loader for Wake-Up Coach service."""

import os
from typing import Optional
from dataclasses import dataclass


@dataclass
class AsteriskConfig:
    """Asterisk ARI configuration."""
    host: str
    port: int
    username: str
    password: str
    app_name: str

    @property
    def base_url(self) -> str:
        """Get the base URL for ARI requests."""
        return f"http://{self.host}:{self.port}/ari"


@dataclass
class OpenAIConfig:
    """OpenAI API configuration."""
    api_key: str
    model: str


@dataclass
class CallConfig:
    """Call-related configuration."""
    target_phone_number: str
    wake_keyword: str
    wake_up_time: Optional[str] = None  # Time in HH:MM format (24-hour), e.g., "07:00"
    call_immediately: bool = False  # If True, skip waiting for scheduled time (for testing)
    timezone: str = "UTC"  # Timezone for wake-up time, e.g., "America/New_York", "US/Eastern"


@dataclass
class HTTPConfig:
    """HTTP server configuration for audio playback."""
    host: str
    port: int
    external_port: int  # Port accessible from Asterisk (may differ from listen port)


@dataclass
class AsteriskFileConfig:
    """Asterisk file system configuration for audio playback."""
    sounds_dir: str  # Directory on Asterisk server to copy audio files
    ssh_host: str    # SSH hostname/IP for Asterisk server
    ssh_user: str    # SSH username
    ssh_key: str = ""  # Optional SSH key path


@dataclass
class Config:
    """Main application configuration."""
    asterisk: AsteriskConfig
    openai: OpenAIConfig
    call: CallConfig
    http: HTTPConfig
    log_level: str


def load_config() -> Config:
    """
    Load configuration from environment variables.

    Raises:
        ValueError: If required environment variables are missing.
    """
    missing = []

    def get_env(key: str, default: Optional[str] = None, required: bool = True) -> str:
        """Get environment variable or track if missing."""
        value = os.getenv(key, default)
        if value is None and required:
            missing.append(key)
            return ""
        return value if value is not None else ""

    # Load Asterisk configuration
    asterisk = AsteriskConfig(
        host=get_env("ARI_HOST", "localhost"),
        port=int(get_env("ARI_PORT", "8088")),
        username=get_env("ARI_USERNAME"),
        password=get_env("ARI_PASSWORD"),
        app_name=get_env("ARI_APP_NAME", "wakeup-coach")
    )

    # Load OpenAI configuration
    openai = OpenAIConfig(
        api_key=get_env("OPENAI_API_KEY"),
        model=get_env("OPENAI_MODEL", "gpt-4o-realtime-preview-2024-12-17")
    )

    # Load call configuration
    call_immediately = get_env("CALL_IMMEDIATELY", "false", required=False).lower() in ("true", "1", "yes")
    call = CallConfig(
        target_phone_number=get_env("TARGET_PHONE_NUMBER"),
        wake_keyword=get_env("WAKE_KEYWORD", "awake"),
        wake_up_time=get_env("WAKE_UP_TIME", None, required=False) or None,  # Optional: e.g., "07:00"
        call_immediately=call_immediately,
        timezone=get_env("TIMEZONE", "UTC", required=False)  # Optional: e.g., "America/New_York"
    )

    # Load HTTP server configuration
    http = HTTPConfig(
        host=get_env("HTTP_SERVER_HOST", "0.0.0.0"),
        port=int(get_env("HTTP_SERVER_PORT", "8080")),
        external_port=int(get_env("HTTP_SERVER_EXTERNAL_PORT", get_env("HTTP_SERVER_PORT", "8080")))
    )

    # Check for missing required variables
    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Please copy .env.example to .env and fill in the values."
        )

    return Config(
        asterisk=asterisk,
        openai=openai,
        call=call,
        http=http,
        log_level=get_env("LOG_LEVEL", "INFO")
    )
