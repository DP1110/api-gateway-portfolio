"""
Gateway Configuration
================================
Central settings loaded from environment variables via pydantic-settings.
All subsequent steps add fields here rather than scattering env-var
reads across the codebase.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # -------------------------------------------------------------------
    # Step 1 – Basic reverse proxy (fallback if no routes file)
    # -------------------------------------------------------------------
    backend_url: str = "http://localhost:9001"

    # Gateway listen address
    host: str = "0.0.0.0"
    port: int = 8000

    # -------------------------------------------------------------------
    # Step 2 – Dynamic routing
    # -------------------------------------------------------------------
    # Path to the JSON routes config file.  Set to "" to disable file-
    # based routing and fall back to BACKEND_URL for all traffic.
    routes_file: str = "routes.json"
    # How often (seconds) the file watcher checks for config changes.
    routes_poll_interval: float = 5.0

    # -------------------------------------------------------------------
    # Future steps (placeholders – not yet wired up)
    # -------------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = "postgresql://gateway:gateway@localhost:5432/gateway"
    jwt_secret: str = "changeme"
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


# Module-level singleton so other modules can do `from gateway.config import settings`
settings = Settings()
