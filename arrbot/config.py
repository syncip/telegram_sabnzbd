"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    if required and not value:
        raise SystemExit(f"Umgebungsvariable {name} fehlt (in docker-compose.yml unter environment eintragen).")
    return value or None


def _int_list(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip()}


@dataclass
class ArrConfig:
    url: str
    api_key: str
    root_folder: str | None = None
    quality_profile: str | None = None


@dataclass
class LLMConfig:
    provider: str  # gemini | anthropic | openai
    model: str
    api_key: str | None
    base_url: str | None = None


@dataclass
class Config:
    telegram_token: str
    allowed_users: set[int]
    llm: LLMConfig
    radarr: ArrConfig | None
    sonarr: ArrConfig | None
    poll_interval: int = 60
    no_release_minutes: int = 30
    track_days: int = 30
    history_idle_minutes: int = 30
    max_history_messages: int = 40
    minimum_availability: str = "released"
    data_dir: str = "/data"
    extra: dict = field(default_factory=dict)


DEFAULT_MODELS = {
    "gemini": "gemini-flash-latest",
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-4o-mini",
}

KEY_VARS = {
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def load_config() -> Config:
    provider = (_env("LLM_PROVIDER", "gemini") or "gemini").lower()
    if provider not in DEFAULT_MODELS:
        raise SystemExit(f"LLM_PROVIDER muss gemini, anthropic oder openai sein, nicht {provider!r}.")

    api_key = _env("LLM_API_KEY") or _env(KEY_VARS[provider])
    base_url = _env("LLM_BASE_URL")
    if not api_key and not (provider == "openai" and base_url):
        # OpenAI-compatible local servers (Ollama, LM Studio) often need no key.
        raise SystemExit(f"API-Key fehlt: setze LLM_API_KEY oder {KEY_VARS[provider]}.")

    llm = LLMConfig(
        provider=provider,
        model=_env("LLM_MODEL") or DEFAULT_MODELS[provider],
        api_key=api_key,
        base_url=base_url,
    )

    def arr(prefix: str) -> ArrConfig | None:
        url = _env(f"{prefix}_URL")
        key = _env(f"{prefix}_API_KEY")
        if not url or not key:
            return None
        return ArrConfig(
            url=url.rstrip("/"),
            api_key=key,
            root_folder=_env(f"{prefix}_ROOT_FOLDER"),
            quality_profile=_env(f"{prefix}_QUALITY_PROFILE"),
        )

    radarr, sonarr = arr("RADARR"), arr("SONARR")
    if not radarr and not sonarr:
        raise SystemExit("Mindestens RADARR_URL/RADARR_API_KEY oder SONARR_URL/SONARR_API_KEY setzen.")

    allowed = _int_list(_env("ALLOWED_USER_IDS"))
    if not allowed:
        raise SystemExit(
            "ALLOWED_USER_IDS fehlt. Ohne Liste könnte jeder deinen Bot steuern. "
            "Deine ID bekommst du z. B. über @userinfobot."
        )

    return Config(
        telegram_token=_env("TELEGRAM_BOT_TOKEN", required=True),  # type: ignore[arg-type]
        allowed_users=allowed,
        llm=llm,
        radarr=radarr,
        sonarr=sonarr,
        poll_interval=int(_env("POLL_INTERVAL_SECONDS", "60")),  # type: ignore[arg-type]
        no_release_minutes=int(_env("NO_RELEASE_NOTICE_MINUTES", "30")),  # type: ignore[arg-type]
        track_days=int(_env("TRACK_DAYS", "30")),  # type: ignore[arg-type]
        history_idle_minutes=int(_env("CONVERSATION_IDLE_MINUTES", "30")),  # type: ignore[arg-type]
        minimum_availability=_env("RADARR_MINIMUM_AVAILABILITY", "released"),  # type: ignore[arg-type]
        data_dir=_env("DATA_DIR", "/data"),  # type: ignore[arg-type]
    )
