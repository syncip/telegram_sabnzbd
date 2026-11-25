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
    admin_id: int
    allowed_users: set[int]
    llm: LLMConfig
    radarr: ArrConfig | None
    sonarr: ArrConfig | None
    poll_interval: int = 60
    no_release_minutes: int = 30
    track_days: int = 30
    history_idle_minutes: int = 10
    max_history_messages: int = 16
    minimum_availability: str = "released"
    quality_prompt_seconds: int = 60
    data_dir: str = "/data"
    sabnzbd_url: str | None = None
    sabnzbd_api_key: str | None = None
    timezone: str = "Europe/Berlin"
    tmdb_api_key: str | None = None
    plex_url: str | None = None  # optional: local address of the Plex server, e.g. http://192.168.1.10:32400
    disk_warn_gb: int = 50  # warn admins below this much free space; 0 = off
    unwatched_days: int = 180  # media check: "on the server for a long time" means this many days
    media_paths: str | None = None  # 🎧 listening: "/tv=/media/tv;/movies=/media/movies" (Sonarr path = bot path)
    listen_api_key: str | None = None  # 🎧 listening: OpenAI key; empty = the AI key if the AI is OpenAI
    site_hours: int = 6  # watched websites: look this often; 0 = off
    monitor_minutes: int = 5  # check Radarr/Sonarr/SABnzbd/TMDB/Plex this often, tell admins on outage; 0 = off
    update_repo: str = "syncip/telegram_sabnzbd"  # GitHub repo for the update check; empty = off
    update_token: str | None = None  # read-only GitHub token, only for a private repo
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
    admin_raw = _env("ADMIN_USER_ID")
    if admin_raw:
        admin_id = int(admin_raw)
    elif len(allowed) == 1:
        admin_id = next(iter(allowed))  # older setups: the single allowed user becomes admin
    else:
        raise SystemExit(
            "ADMIN_USER_ID fehlt. Trag deine Telegram-ID ein (z. B. über @userinfobot herausfinden). "
            "Weitere Nutzer gibst du dann im Bot frei."
        )

    sab_url, sab_key = _env("SABNZBD_URL"), _env("SABNZBD_API_KEY")
    if bool(sab_url) != bool(sab_key):
        raise SystemExit("Für SABnzbd bitte SABNZBD_URL und SABNZBD_API_KEY beide setzen (oder beide leer).")

    return Config(
        telegram_token=_env("TELEGRAM_BOT_TOKEN", required=True),  # type: ignore[arg-type]
        admin_id=admin_id,
        allowed_users=allowed,
        sabnzbd_url=sab_url,
        sabnzbd_api_key=sab_key,
        timezone=_env("TZ", "Europe/Berlin"),  # type: ignore[arg-type]
        tmdb_api_key=_env("TMDB_API_KEY"),
        plex_url=_env("PLEX_URL"),
        disk_warn_gb=int(_env("DISK_WARN_GB", "50")),  # type: ignore[arg-type]
        unwatched_days=int(_env("UNWATCHED_DAYS", "180")),  # type: ignore[arg-type]
        media_paths=_env("MEDIA_PATHS"),
        listen_api_key=_env("LISTEN_API_KEY") or (llm.api_key if llm.provider == "openai" else None),
        site_hours=int(_env("SITE_CHECK_HOURS", "6")),  # type: ignore[arg-type]
        monitor_minutes=int(_env("MONITOR_MINUTES", "5")),  # type: ignore[arg-type]
        update_repo=_env("UPDATE_REPO", "syncip/telegram_sabnzbd") or "",  # type: ignore[arg-type]
        update_token=_env("UPDATE_TOKEN"),
        llm=llm,
        radarr=radarr,
        sonarr=sonarr,
        poll_interval=int(_env("POLL_INTERVAL_SECONDS", "60")),  # type: ignore[arg-type]
        no_release_minutes=int(_env("NO_RELEASE_NOTICE_MINUTES", "30")),  # type: ignore[arg-type]
        track_days=int(_env("TRACK_DAYS", "30")),  # type: ignore[arg-type]
        history_idle_minutes=int(_env("CONVERSATION_IDLE_MINUTES", "10")),  # type: ignore[arg-type]
        minimum_availability=_env("RADARR_MINIMUM_AVAILABILITY", "released"),  # type: ignore[arg-type]
        quality_prompt_seconds=int(_env("QUALITY_PROMPT_SECONDS", "60")),  # type: ignore[arg-type]
        data_dir=_env("DATA_DIR", "/data"),  # type: ignore[arg-type]
    )
