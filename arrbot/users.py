"""Who may use the bot: admin, users, pending requests and blocked IDs (data/users.json)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .state import utcnow

log = logging.getLogger(__name__)

ADMIN, USER, PENDING, BLOCKED = "admin", "user", "pending", "blocked"

# Limit keys (German, as typed in /limit): requests per day/month, USD and tokens per month.
LIMIT_KEYS = {"tag": "Anfragen pro Tag", "monat": "Anfragen pro Monat", "kosten": "Kosten pro Monat ($)",
              "tokens": "Tokens pro Monat"}
TOTAL = "gesamt"


def _period() -> tuple[str, str]:
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


@dataclass
class Usage:
    day: str = ""
    day_requests: int = 0
    month: str = ""
    month_requests: int = 0
    month_tokens: int = 0
    month_cost: float = 0.0

    def roll(self) -> None:
        day, month = _period()
        if self.day != day:
            self.day, self.day_requests = day, 0
        if self.month != month:
            self.month, self.month_requests, self.month_tokens, self.month_cost = month, 0, 0, 0.0

    def value(self, key: str) -> float:
        self.roll()
        return {"tag": self.day_requests, "monat": self.month_requests, "kosten": self.month_cost,
                "tokens": self.month_tokens}[key]


@dataclass
class User:
    id: int
    name: str
    role: str  # admin | user | pending | blocked
    since: str
    limits: dict = field(default_factory=dict)  # own limits; missing keys fall back to the defaults
    usage: Usage = field(default_factory=Usage)


class UserStore:
    def __init__(self, data_dir: str, admin_id: int, seed_users: set[int] = frozenset()):  # type: ignore[assignment]
        self.path = Path(data_dir) / "users.json"
        self.admin_id = admin_id
        self.defaults: dict = {}  # limits for users without own values (never applied to admins)
        self.total_limits: dict = {}  # whole bot, e.g. {"kosten": 1.0}
        self.total = Usage()
        self.users: dict[int, User] = self._load()
        # The configured admin is always admin; IDs from ALLOWED_USER_IDS are plain users.
        self._ensure(admin_id, ADMIN)
        for uid in seed_users:
            if uid != admin_id and self.role(uid) in (None, PENDING):
                self._ensure(uid, USER)
        self.save()

    def _load(self) -> dict[int, User]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if "users" not in raw:  # first version: plain {id: user}
                raw = {"users": raw}
            self.defaults = raw.get("defaults") or {}
            self.total_limits = raw.get("total_limits") or {}
            self.total = Usage(**(raw.get("total") or {}))
            users = {}
            for k, v in raw["users"].items():
                v = dict(v)
                v["usage"] = Usage(**(v.get("usage") or {}))
                users[int(k)] = User(**v)
            return users
        except (ValueError, TypeError) as e:
            log.error("users.json unlesbar (%s), starte nur mit Admin", e)
            return {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({
                    "users": {str(k): asdict(v) for k, v in self.users.items()},
                    "defaults": self.defaults,
                    "total_limits": self.total_limits,
                    "total": asdict(self.total),
                }, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("Kann %s nicht schreiben: %s", self.path, e)

    def _ensure(self, uid: int, role: str, name: str = "") -> User:
        u = self.users.get(uid)
        if u is None:
            u = self.users[uid] = User(uid, name or str(uid), role, utcnow().isoformat())
        else:
            u.role = role
            if name:
                u.name = name
        if uid == self.admin_id:
            u.role = ADMIN
        return u

    # ---- queries ----------------------------------------------------------
    def role(self, uid: int) -> str | None:
        u = self.users.get(uid)
        return u.role if u else None

    def is_allowed(self, uid: int) -> bool:
        return self.role(uid) in (ADMIN, USER)

    def is_admin(self, uid: int) -> bool:
        return self.role(uid) == ADMIN

    def name(self, uid: int) -> str:
        u = self.users.get(uid)
        return u.name if u else str(uid)

    def admins(self) -> list[int]:
        return [u.id for u in self.users.values() if u.role == ADMIN]

    def listing(self) -> list[User]:
        order = {ADMIN: 0, USER: 1, PENDING: 2, BLOCKED: 3}
        return sorted(self.users.values(), key=lambda u: (order.get(u.role, 9), u.name.lower()))

    # ---- changes ------------------------------------------------------------
    def touch_name(self, uid: int, name: str) -> None:
        u = self.users.get(uid)
        if u and name and u.name != name:
            u.name = name
            self.save()

    def request(self, uid: int, name: str) -> bool:
        """Unknown user asks for access. True if the admin should be asked (first request only)."""
        if self.role(uid) is not None:
            return False
        self._ensure(uid, PENDING, name)
        self.save()
        return True

    def set_role(self, uid: int, role: str, name: str = "") -> User:
        if uid == self.admin_id and role != ADMIN:
            raise ValueError("Der Haupt-Admin (ADMIN_USER_ID) kann nicht geändert werden.")
        u = self._ensure(uid, role, name)
        self.save()
        return u

    def remove(self, uid: int) -> User | None:
        if uid == self.admin_id:
            raise ValueError("Der Haupt-Admin (ADMIN_USER_ID) kann nicht entfernt werden.")
        u = self.users.pop(uid, None)
        self.save()
        return u

    # ---- limits -------------------------------------------------------------------
    def limits_for(self, uid: int) -> dict:
        u = self.users.get(uid)
        if not u:
            return {}
        base = {} if u.role == ADMIN else dict(self.defaults)
        base.update(u.limits)
        return {k: v for k, v in base.items() if v is not None}

    def blocked_by_limit(self, uid: int) -> str | None:
        """Reason text if uid may not send another AI request right now."""
        for key, limit in self.total_limits.items():
            if self.total.value(key) >= limit:
                return f"Das Gesamtbudget des Bots ist für diesen Monat aufgebraucht ({_fmt(key, limit)})."
        u = self.users.get(uid)
        if not u:
            return None
        for key, limit in self.limits_for(uid).items():
            if u.usage.value(key) >= limit:
                when = "heute" if key == "tag" else "diesen Monat"
                return f"Dein Limit ist {when} erreicht: {_fmt(key, limit)}."
        return None

    def count_request(self, uid: int) -> None:
        for usage in self._usages(uid):
            usage.roll()
            usage.day_requests += 1
            usage.month_requests += 1
        self.save()

    def add_usage(self, uid: int, tokens: int, cost: float | None) -> None:
        for usage in self._usages(uid):
            usage.roll()
            usage.month_tokens += tokens
            usage.month_cost += cost or 0.0
        self.save()

    def _usages(self, uid: int) -> list[Usage]:
        u = self.users.get(uid)
        return [self.total] + ([u.usage] if u else [])

    def set_limit(self, target: str | int, key: str, value: float | None) -> None:
        """target: user id, 'standard' (default for all users) or 'gesamt' (whole bot)."""
        if key not in LIMIT_KEYS:
            raise ValueError(f"Unbekanntes Limit {key!r}. Möglich: {', '.join(LIMIT_KEYS)}")
        if target == "standard":
            d = self.defaults
        elif target == TOTAL:
            d = self.total_limits
        else:
            u = self.users.get(int(target))
            if not u:
                raise ValueError(f"Nutzer {target} kenne ich nicht.")
            d = u.limits
        if value is None:
            d.pop(key, None)
        else:
            d[key] = value
        self.save()

    def find(self, text: str) -> int | None:
        """User by id or (part of the) name."""
        if text.lstrip("-").isdigit():
            return int(text)
        hits = [u.id for u in self.users.values() if text.lower().lstrip("@") in u.name.lower()]
        return hits[0] if len(hits) == 1 else None


def _fmt(key: str, value: float) -> str:
    if key == "kosten":
        return f"{value:.2f} $ pro Monat".replace(".", ",")
    if key == "tokens":
        return f"{int(value):,} Tokens pro Monat".replace(",", ".")
    return f"{int(value)} Anfragen pro {'Tag' if key == 'tag' else 'Monat'}"


def limit_text(key: str, value: float) -> str:
    return _fmt(key, value)
