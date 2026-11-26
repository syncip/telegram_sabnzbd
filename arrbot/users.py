"""Who may use the bot: admin, users, pending requests and blocked IDs (data/users.json)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import utcnow

log = logging.getLogger(__name__)

ADMIN, USER, PENDING, BLOCKED = "admin", "user", "pending", "blocked"

# Limit keys (German, as typed in /limit): requests per day/month, USD and tokens per month.
AI_KEYS = {"tag": "Anfragen pro Tag", "monat": "Anfragen pro Monat", "kosten": "Kosten pro Monat ($)",
           "tokens": "Tokens pro Monat", "tokens_tag": "Tokens pro Tag", "kosten_tag": "Kosten pro Tag ($)"}
DAYS_KEEP = 60
WARN_AT = 0.8  # the admins hear about it when the bot's total use passes 80 % of a limit
# What a user may add, per period: "filme_tag" = movies per day, "folgen_woche" = episodes per week, …
TITLE_WHAT = {"filme": "Filme", "serien": "Serien", "staffeln": "Staffeln", "folgen": "Folgen"}
PERIODS = {"stunde": ("Stunde", 3600), "tag": ("Tag", 86400), "woche": ("Woche", 7 * 86400),
           "monat": ("Monat", 30 * 86400)}
TITLE_KEYS = {f"{w}_{p}": f"{lw} pro {lp}" for w, lw in TITLE_WHAT.items() for p, (lp, _) in PERIODS.items()}
LIMIT_KEYS = {**AI_KEYS, **TITLE_KEYS}
TOTAL = "gesamt"
ADDS_KEEP_DAYS = 31

# What an admin can allow single users ("Sarah darf ihre Filme und Serien löschen").
PERMS = {
    "delete_own": "eigene Filme/Serien löschen (nur, was sie/er selbst angefragt hat)",
    "delete": "alle Filme/Serien löschen",
    "pause": "Suche pausieren und fortsetzen (eigene Titel)",
    "repair": "Deutsch-Reparatur und Folgen aufräumen",
    "media": "Medien-Manager: aufräumen, Duplikate, Speicher (alle Titel!)",
    "sites": "Webseiten beobachten",
    "no_limits": "keine Limits (Anfragen und Titel)",
}


def _period() -> tuple[str, str]:
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


MONTHS_KEEP = 12


@dataclass
class Usage:
    day: str = ""
    day_requests: int = 0
    day_tokens: int = 0
    day_cost: float = 0.0
    month: str = ""
    month_requests: int = 0
    month_tokens: int = 0
    month_cost: float = 0.0
    month_calls: int = 0
    total_requests: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    total_calls: int = 0
    last_active: str = ""
    # finished months: {"2026-10": {"requests": 80, "tokens": 250000, "cost": 0.05, "calls": 170}}
    months: dict = field(default_factory=dict)
    # finished days (for limit advice): {"2026-11-25": {"requests": 12, "tokens": 40000, "cost": 0.01}}
    days: dict = field(default_factory=dict)

    def roll(self) -> None:
        day, month = _period()
        if self.day != day:
            if self.day and (self.day_requests or self.day_tokens):
                self.days[self.day] = {"requests": self.day_requests, "tokens": self.day_tokens,
                                       "cost": round(self.day_cost, 6)}
                for old in sorted(self.days)[:-DAYS_KEEP]:
                    del self.days[old]
            self.day, self.day_requests, self.day_tokens, self.day_cost = day, 0, 0, 0.0
        if self.month != month:
            if self.month and (self.month_requests or self.month_tokens):
                self.months[self.month] = {"requests": self.month_requests, "tokens": self.month_tokens,
                                           "cost": round(self.month_cost, 6), "calls": self.month_calls}
                for old in sorted(self.months)[:-MONTHS_KEEP]:
                    del self.months[old]
            self.month, self.month_requests, self.month_tokens, self.month_cost = month, 0, 0, 0.0
            self.month_calls = 0

    def value(self, key: str) -> float:
        self.roll()
        return {"tag": self.day_requests, "monat": self.month_requests, "kosten": self.month_cost,
                "tokens": self.month_tokens, "tokens_tag": self.day_tokens, "kosten_tag": self.day_cost}[key]


@dataclass
class User:
    id: int
    name: str
    role: str  # admin | user | pending | blocked
    since: str
    limits: dict = field(default_factory=dict)  # own limits; missing keys fall back to the defaults
    usage: Usage = field(default_factory=Usage)
    quality: int = 0  # max resolution (720/1080/2160) for this user; 0 = the standard for all users
    perms: list[str] = field(default_factory=list)  # extra rights, see PERMS
    adds: list[dict] = field(default_factory=list)  # {"at": iso, "filme": 1, ...} of the last ADDS_KEEP_DAYS


class UserStore:
    on_remove = None  # (uid) -> None, set by the bot
    on_role = None  # (uid, old role, new role) -> None, set by the bot

    def __init__(self, data_dir: str, admin_id: int, seed_users: set[int] = frozenset()):  # type: ignore[assignment]
        self.path = Path(data_dir) / "users.json"
        self.admin_id = admin_id
        self.defaults: dict = {}  # limits for users without own values (never applied to admins)
        self.total_limits: dict = {}  # whole bot, e.g. {"kosten": 1.0}
        self.total = Usage()
        self.default_quality = 0  # max resolution for all users (0 = no limit)
        self.warned: set[str] = set()  # total limit warnings already sent (in memory)
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
            self.default_quality = int(raw.get("default_quality") or 0)
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
                    "default_quality": self.default_quality,
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
        old = self.role(uid)
        u = self._ensure(uid, role, name)
        self.save()
        if self.on_role and old != u.role:
            self.on_role(uid, old, u.role)
        return u

    def remove(self, uid: int) -> User | None:
        if uid == self.admin_id:
            raise ValueError("Der Haupt-Admin (ADMIN_USER_ID) kann nicht entfernt werden.")
        u = self.users.pop(uid, None)
        self.save()
        if u and self.on_remove:
            self.on_remove(uid)  # their profile goes too
        return u

    # ---- limits -------------------------------------------------------------------
    def limits_for(self, uid: int) -> dict:
        u = self.users.get(uid)
        if not u:
            return {}
        base = {} if u.role == ADMIN else dict(self.defaults)
        base.update(u.limits)
        return {k: v for k, v in base.items() if v is not None}

    # ---- rights -------------------------------------------------------------------
    def has_perm(self, uid: int, perm: str) -> bool:
        u = self.users.get(uid)
        if not u or u.role not in (ADMIN, USER):
            return False
        return u.role == ADMIN or perm in u.perms

    def apply_perms(self, who: str, grant: list[str] | None = None, revoke: list[str] | None = None) -> str:
        uid = self.find(who)
        if uid is None:
            raise ValueError(f"Nutzer „{who}“ kenne ich nicht. /nutzer zeigt alle.")
        u = self.users[uid]
        if u.role == ADMIN:
            return f"👑 {u.name} ist Admin und darf ohnehin alles."
        bad = [p for p in (grant or []) + (revoke or []) if p not in PERMS]
        if bad:
            raise ValueError(f"Unbekannte Erlaubnis: {', '.join(bad)}. Möglich: {', '.join(PERMS)}")
        for p in grant or []:
            if p not in u.perms:
                u.perms.append(p)
        u.perms = [p for p in u.perms if p not in (revoke or [])]
        self.save()
        return perms_text(u)

    # ---- limits for titles ("3 Filme pro Tag") ------------------------------------------
    def title_limit(self, uid: int, now: datetime | None = None, **want: int) -> str | None:
        """Reason text if uid may not add this now. want: filme / serien / staffeln / folgen counts."""
        u = self.users.get(uid)
        if not u or u.role == ADMIN or "no_limits" in u.perms:
            return None
        now = now or datetime.now(timezone.utc)
        for key, limit in self.limits_for(uid).items():
            if key not in TITLE_KEYS:
                continue
            what, period = key.split("_")
            new = int(want.get(what) or 0)
            if not new:
                continue
            seconds = PERIODS[period][1]
            recent = [a for a in u.adds if a.get(what) and _ts(a["at"]) > now - timedelta(seconds=seconds)]
            used = sum(int(a[what]) for a in recent)
            if used + new <= limit:
                continue
            label = TITLE_KEYS[key]
            if used >= limit:
                oldest = min(_ts(a["at"]) for a in recent) if recent else now
                wait = oldest + timedelta(seconds=seconds) - now
                return (f"Dein Limit ist erreicht: höchstens {limit:g} {label} (schon {used}). "
                        f"Wieder möglich in {_wait_text(wait)}.")
            return (f"Das sind {new} {TITLE_WHAT[what]} – dein Limit erlaubt gerade noch {int(limit - used)} "
                    f"({limit:g} {label}). Weniger auswählen, z. B. nur eine Staffel?")
        return None

    def record_adds(self, uid: int, now: datetime | None = None, **counts: int) -> None:
        u = self.users.get(uid)
        if not u:
            return
        now = now or datetime.now(timezone.utc)
        entry = {k: int(v) for k, v in counts.items() if k in TITLE_WHAT and v}
        if not entry:
            return
        entry["at"] = now.isoformat()
        cutoff = now - timedelta(days=ADDS_KEEP_DAYS)
        u.adds = [a for a in u.adds if _ts(a["at"]) > cutoff] + [entry]
        self.save()

    def blocked_by_limit(self, uid: int) -> str | None:
        """Reason text if uid may not send another AI request right now."""
        for key, limit in self.total_limits.items():
            if key in AI_KEYS and self.total.value(key) >= limit:
                when = "heute" if _daily(key) else "diesen Monat"
                return f"Das Gesamtbudget des Bots ist für {when} aufgebraucht ({_fmt(key, limit)})."
        u = self.users.get(uid)
        if not u:
            return None
        if "no_limits" in u.perms:
            return None
        for key, limit in self.limits_for(uid).items():
            if key in AI_KEYS and u.usage.value(key) >= limit:
                when = "heute" if _daily(key) else "diesen Monat"
                return f"Dein Limit ist {when} erreicht: {_fmt(key, limit)}."
        return None

    def total_warnings(self) -> list[str]:
        """Once per day/month and limit: the bot's total use passed 80 % or 100 %."""
        out = []
        for key, limit in self.total_limits.items():
            if key not in AI_KEYS or not limit:
                continue
            used = self.total.value(key)
            period = self.total.day if _daily(key) else self.total.month
            for level in (1.0, WARN_AT):
                mark = f"{key}:{period}:{level}"
                if used >= limit * level:
                    if mark not in self.warned:
                        self.warned.add(mark)
                        if level == WARN_AT and f"{key}:{period}:1.0" in self.warned:
                            break
                        when = "heute" if _daily(key) else "diesen Monat"
                        out.append(f"{'⛔' if level == 1.0 else '⚠️'} Gesamtbudget {when}: {int(used / limit * 100)} % "
                                   f"von {_fmt(key, limit)} verbraucht"
                                   + (" – der Bot antwortet bis dahin niemandem mehr." if level == 1.0 else "."))
                    break
        return out

    def count_request(self, uid: int) -> None:
        now = utcnow().isoformat()
        for usage in self._usages(uid):
            usage.roll()
            usage.day_requests += 1
            usage.month_requests += 1
            usage.total_requests += 1
            usage.last_active = now
        self.save()

    def add_usage(self, uid: int, tokens: int, cost: float | None) -> None:
        """One AI call."""
        for usage in self._usages(uid):
            usage.roll()
            usage.month_tokens += tokens
            usage.month_cost += cost or 0.0
            usage.day_tokens += tokens
            usage.day_cost += cost or 0.0
            usage.month_calls += 1
            usage.total_tokens += tokens
            usage.total_cost += cost or 0.0
            usage.total_calls += 1
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
            if key not in AI_KEYS:
                raise ValueError("Titel-Limits gibt es pro Nutzer oder als Standard für alle, nicht für den ganzen Bot.")
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

    def apply_limit(self, who: str, key: str, raw: str | float | None) -> str:
        """Shared by /limit and the admin AI tool. Returns the confirmation; ValueError with a readable text."""
        key = (key or "").lower().strip()
        if key not in LIMIT_KEYS:
            raise ValueError(f"Unbekannte Art {key!r}. Möglich: {', '.join(LIMIT_KEYS)}.")
        who_l = str(who).lower().strip()
        if who_l in ("standard", "alle", "default"):
            target: str | int = "standard"
            label = "alle Nutzer (Standard)"
        elif who_l in (TOTAL, "bot", "gesamtbudget"):
            target, label = TOTAL, "den ganzen Bot"
        else:
            found = self.find(str(who))
            if found is None or found not in self.users:
                raise ValueError(f"Nutzer „{who}“ nicht eindeutig gefunden. Nimm die ID aus /nutzer.")
            target, label = found, self.name(found)
        if raw is None or str(raw).lower().strip() in ("aus", "off", "0", "-", "none", "kein", "keins"):
            value = None
        else:
            try:
                value = float(str(raw).replace(",", ".").replace("$", "").strip())
            except ValueError:
                raise ValueError(f"„{raw}“ ist keine Zahl. Beispiel: /limit standard tag 10") from None
            if value < 0:
                raise ValueError("Limits müssen positiv sein.")
        self.set_limit(target, key, value)
        txt = f"{LIMIT_KEYS[key]} aus" if value is None else limit_text(key, value)
        return f"👍 Für {label}: {txt}."

    def max_quality(self, uid: int) -> int:
        """Highest resolution uid may request (0 = any). Admins have no limit."""
        u = self.users.get(uid)
        if u is None or u.role == ADMIN:
            return 0
        return u.quality or self.default_quality

    def apply_quality(self, who: str, raw: str | int | None) -> str:
        """Max quality for one user or 'standard' (all). raw: 480/720/1080/2160, SD/HD/FullHD/4K or 'aus'."""
        value = parse_resolution(raw)
        who_l = str(who).lower().strip()
        if who_l in ("standard", "alle", "default", "gruppe"):
            self.default_quality, label = value, "alle Nutzer (Standard)"
        else:
            uid = self.find(str(who))
            if uid is None or uid not in self.users:
                raise ValueError(f"Nutzer „{who}“ nicht eindeutig gefunden. Nimm die ID aus /nutzer.")
            if self.users[uid].role == ADMIN:
                raise ValueError("Admins haben keine Qualitätsgrenze.")
            self.users[uid].quality, label = value, self.name(uid)
        self.save()
        return f"👍 Für {label}: " + (f"höchstens {quality_label(value)}." if value else "keine Qualitätsgrenze"
                                      + (" (es gilt der Standard)." if label != "alle Nutzer (Standard)" else "."))

    def apply_role(self, who: str, action: str) -> tuple[int, str]:
        """freigeben / sperren / entfernen by name or id. Returns (user id, confirmation)."""
        uid = self.find(str(who))
        if uid is None or uid not in self.users:
            raise ValueError(f"Nutzer „{who}“ nicht eindeutig gefunden.")
        name = self.name(uid)
        action = action.lower()
        if action in ("freigeben", "allow", "ok"):
            self.set_role(uid, USER)
            return uid, f"✅ {name} ist freigegeben."
        if action in ("sperren", "block"):
            self.set_role(uid, BLOCKED)
            return uid, f"🚫 {name} ist gesperrt."
        if action in ("entfernen", "remove", "löschen"):
            self.remove(uid)
            return uid, f"🗑 {name} wurde entfernt (kann neu anfragen)."
        raise ValueError("Aktion muss freigeben, sperren oder entfernen sein.")

    def find(self, text: str) -> int | None:
        """User by id or (part of the) name."""
        if text.lstrip("-").isdigit():
            return int(text)
        hits = [u.id for u in self.users.values() if text.lower().lstrip("@") in u.name.lower()]
        return hits[0] if len(hits) == 1 else None


def _daily(key: str) -> bool:
    return key == "tag" or key.endswith("_tag")


def _ts(raw: str) -> datetime:
    d = datetime.fromisoformat(raw)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _wait_text(td: timedelta) -> str:
    s = max(60, int(td.total_seconds()))
    if s < 3600:
        return f"{s // 60} Min."
    if s < 2 * 86400:
        return f"{round(s / 3600)} Std."
    return f"{round(s / 86400)} Tagen"


def perms_text(u: User) -> str:
    if not u.perms:
        return f"🔐 {u.name} darf suchen und hinzufügen – sonst nichts Besonderes."
    return f"🔐 {u.name} darf zusätzlich:\n" + "\n".join(f"• {PERMS.get(p, p)}" for p in u.perms)


def _fmt(key: str, value: float) -> str:
    if key in ("kosten", "kosten_tag"):
        return f"{value:.2f} $ pro {'Tag' if key == 'kosten_tag' else 'Monat'}".replace(".", ",")
    if key in ("tokens", "tokens_tag"):
        return f"{int(value):,} Tokens pro {'Tag' if key == 'tokens_tag' else 'Monat'}".replace(",", ".")
    if key in TITLE_KEYS:
        what, period = key.split("_")
        return f"höchstens {int(value)} {TITLE_WHAT[what]} pro {PERIODS[period][0]}"
    return f"{int(value)} Anfragen pro {'Tag' if key == 'tag' else 'Monat'}"


def limit_text(key: str, value: float) -> str:
    return _fmt(key, value)


QUALITY_NAMES = {480: "SD", 576: "SD", 720: "HD (720p)", 1080: "Full HD (1080p)", 2160: "4K (2160p)"}


def quality_label(res: int) -> str:
    return QUALITY_NAMES.get(res, f"{res}p")


def parse_resolution(raw: str | int | None) -> int:
    t = str(raw or "").lower().replace(" ", "").replace("-", "")
    if t in ("", "aus", "off", "0", "keine", "kein", "none", "unbegrenzt"):
        return 0
    words = {"sd": 480, "dvd": 480, "hd": 720, "720p": 720, "fullhd": 1080, "fhd": 1080, "1080p": 1080,
             "4k": 2160, "uhd": 2160, "2160p": 2160}
    if t in words:
        return words[t]
    if t.isdigit() and int(t) in (480, 576, 720, 1080, 2160):
        return int(t)
    raise ValueError(f"„{raw}“ verstehe ich nicht als Qualität. Möglich: SD, 720, 1080, 4K oder aus.")
