"""Central configuration: every tunable in one place, read once from the env.

Nothing here touches the network or Telegram at import time, so the app boots
even when env vars are missing or wrong (that was blocker B2 in ANALYSIS.md).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    # --- Telegram (optional; only for the fallback client) ---
    tg_api_id_raw: str = field(default_factory=lambda: _str("TELEGRAM_API_ID"))
    tg_api_hash: str = field(default_factory=lambda: _str("TELEGRAM_API_HASH"))
    tg_session_string: str = field(default_factory=lambda: _str("TELEGRAM_SESSION_STRING"))
    tg_source_order: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s.strip().lower()
            for s in _str("TELEGRAM_SOURCE_ORDER", "preview,telethon").split(",")
            if s.strip()
        )
    )

    # --- LLM defaults (optional) ---
    llm_base_url: str = field(default_factory=lambda: _str("LLM_BASE_URL"))
    llm_api_key: str = field(default_factory=lambda: _str("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _str("LLM_MODEL_NAME"))
    allow_server_llm_fallback: bool = field(
        default_factory=lambda: _bool("ALLOW_SERVER_LLM_FALLBACK", False)
    )

    # --- Batching ---
    batch_size: int = field(default_factory=lambda: _int("BATCH_SIZE", 15))
    max_concurrent_llm_calls: int = field(
        default_factory=lambda: max(1, _int("MAX_CONCURRENT_LLM_CALLS", 3))
    )
    max_posts_per_scan: int = field(default_factory=lambda: _int("MAX_POSTS_PER_SCAN", 600))
    max_lookback_days: int = field(default_factory=lambda: _int("MAX_LOOKBACK_DAYS", 120))
    max_batch_chars: int = field(default_factory=lambda: _int("MAX_BATCH_CHARS", 40_000))
    max_chars_per_post: int = field(default_factory=lambda: _int("MAX_CHARS_PER_POST", 6_000))
    dedupe_by_text: bool = field(default_factory=lambda: _bool("DEDUPE_BY_TEXT", True))

    # --- Abuse protection ---
    rate_limit_scans_per_hour: int = field(
        default_factory=lambda: _int("RATE_LIMIT_SCANS_PER_HOUR", 6)
    )
    rate_limit_ip_concurrency: int = field(
        default_factory=lambda: max(1, _int("RATE_LIMIT_IP_CONCURRENCY", 1))
    )
    global_concurrency: int = field(default_factory=lambda: max(1, _int("GLOBAL_CONCURRENCY", 4)))
    allow_insecure_llm_urls: bool = field(
        default_factory=lambda: _bool("ALLOW_INSECURE_LLM_URLS", False)
    )

    # --- App ---
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())

    # ---------- derived helpers ----------
    @property
    def tg_api_id(self) -> int | None:
        try:
            return int(self.tg_api_id_raw)
        except (TypeError, ValueError):
            return None

    @property
    def telethon_ready(self) -> bool:
        """True when a real, non-interactive Telethon login is possible."""
        return bool(self.tg_api_id and self.tg_api_hash and self.tg_session_string)

    @property
    def server_llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def public_llm_defaults(self) -> dict:
        """Safe-to-expose provider defaults: base URL and model only, never the key."""
        return {
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
        }

    def describe(self) -> dict:
        """Everything /health and the UI need to know, with zero secrets."""
        return {
            "telegram_mode": "telethon+preview" if self.telethon_ready else "preview-only",
            "telethon_ready": self.telethon_ready,
            "allow_server_llm_fallback": self.allow_server_llm_fallback,
            "server_llm_ready": self.server_llm_ready,
            "batch_size": self.batch_size,
            "max_concurrent_llm_calls": self.max_concurrent_llm_calls,
            "max_posts_per_scan": self.max_posts_per_scan,
            "max_lookback_days": self.max_lookback_days,
            "rate_limit_scans_per_hour": self.rate_limit_scans_per_hour,
        }


settings = Settings()
