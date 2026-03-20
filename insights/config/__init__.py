"""Configuration module for WALLE Insights"""

from .settings import Settings, get_settings

# Convenience: global settings instance
settings = get_settings()

__all__ = ["Settings", "get_settings", "settings"]
