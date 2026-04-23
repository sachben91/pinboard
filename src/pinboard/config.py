"""Config loading from ~/.pinboard/config.toml."""

from __future__ import annotations

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore
from dataclasses import dataclass, field
from pathlib import Path


PINBOARD_DIR = Path.home() / ".pinboard"
CONFIG_PATH = PINBOARD_DIR / "config.toml"
_GDRIVE_DB = (
    Path.home()
    / "Library/CloudStorage/GoogleDrive-sachben91@gmail.com/My Drive/pinboard/pinboard.db"
)
DB_PATH = _GDRIVE_DB if _GDRIVE_DB.parent.exists() else PINBOARD_DIR / "pinboard.db"
ARTIFACTS_DIR = PINBOARD_DIR / "artifacts"


@dataclass
class Config:
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    embedding_provider: str = "openai"   # "openai" | "local"
    embedding_model: str = "text-embedding-3-small"
    llm_model: str = "claude-sonnet-4-6"
    connection_threshold: float = 0.55
    half_life_days: float = 14.0
    editor: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            return cls()
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        return cls(
            openai_api_key=data.get("openai_api_key", ""),
            anthropic_api_key=data.get("anthropic_api_key", ""),
            embedding_provider=data.get("embedding_provider", "openai"),
            embedding_model=data.get("embedding_model", "text-embedding-3-small"),
            llm_model=data.get("llm_model", "claude-sonnet-4-6"),
            connection_threshold=float(data.get("connection_threshold", 0.55)),
            half_life_days=float(data.get("half_life_days", 14.0)),
            editor=data.get("editor", ""),
            extra=data,
        )

    def effective_editor(self) -> str:
        import os
        return self.editor or os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
