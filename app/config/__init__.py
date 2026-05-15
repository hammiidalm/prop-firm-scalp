"""Configuration package: typed Pydantic settings loaded from env/.env."""

from app.config.settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
