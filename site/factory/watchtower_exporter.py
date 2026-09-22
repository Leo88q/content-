#!/usr/bin/env python3
"""Watchtower Exporter & Read-Only API для Games Watchtower (app_id: trafficgen).

Контракт: PROMPT_TRAFFIC_GENERATOR_INTEGRATION.md / WATCHTOWER_INTEGRATION.md.

Гарантии этого модуля:
- Канонический off-chain конверт события и identity
  offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:<seq>
- Идемпотентность: UNIQUE(eventId) + UNIQUE(identity), дубликаты не считаются
- Schema-rejected: события без eventType, с seq < 1, с не-UTC временем или
  неизвестным sourceType отклоняются и попадают в trafficgen_events_rejected_total
- Gap detection: DataGapDetected при разрыве seq и DataGapHealed при закрытии
  разрыва реальным backfill'ом; алерты active/resolved
- Курсорная пагинация base64 'cursor:<lastId>', replay с нуля, backfill
- Strict read-only: любой пишущий метод к /watchtower/* -> 405 + Allow: GET, OPTIONS
- PII scrub до записи в БД: запрещённые ключи, IP/email-литералы, wallet-ключи, URL query
- Bot-маркировка: factory_pipeline всегда sourceType=bot
- Синтетика: payload.synthetic=true исключается из продуктовых агрегатов
- Retention 30d + прайнинг; счётчики переживают перезапуск (таблица metrics_state)
- Честная воронка: только реальные события, null вместо выдуманных конверсий (R3)
"""
import base64
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config

DB_PATH = os.path.join(config.DATA_DIR, "watchtower.db")
PARSER_VERSION = "trafficgen-v1"
APP_ID = "trafficgen"
SOURCE_EXPORTER = "trafficgen-exporter"

EVENT_RETENTION_DAYS = 30
AGGREGATE_RETENTION_DAYS = 180
MAX_TRACK_BYTES = 256 * 1024
RATE_LIMIT_RPS = float(os.environ.get("WATCHTOWER_TRACK_RPS", "30"))
RATE_LIMIT_BURST = float(os.environ.get("WATCHTOWER_TRACK_BURST", "30"))
KNOWN_EVENT_TYPE_LABEL_CAP = 20  # cardinality cap для метки event_type

READ_METHODS = "GET, OPTIONS"

# ---------------------------------------------------------------------------
# КАТАЛОГ СОБЫТИЙ (контракт честности: implemented <=> есть реальный эмиттер)
# ---------------------------------------------------------------------------
EVENTS_CATALOG = [
    # (eventType, implemented, reason_if_unavailable)
    ("CampaignCreated", True, None),        # реконсилёрует config store при старте
    ("CampaignStarted", True, None),        # реконсилёрует (status=active)
    ("CampaignStopped", True, None),        # реконсилёрует (смена статуса / удаление из конфига)
    ("CampaignUpdated", True, None),        # реконсилёрует (изменение определения кампании)
    ("SourceConnected", True, None),        # реконсилёрует источники
    ("SourceDisconnected", True, None),     # реконсилёрует источники
    ("SourceHealthChanged", True, None),    # реконсилёрует (смена status)
    ("PageAssigned", True, None),           # реконсилёрует страницы (включая games.js)
    ("PageRemoved", True, None),            # реконсилёрует страницы
    ("SessionStarted", True, None),         # site/app.js при старте сессии
    ("PageView", True, None),               # site/app.js
    ("Click", True, None),                  # site/app.js (общие клики)
    ("CTAClicked", True, None),             # site/app.js (click_slot, promo)
    ("LandingReached", False,
     "Нет подтверждения перехода: нужен redirect-proxy/beacon со стороны игры. Не выдумываем."),
    ("SessionEnded", True, None),           # site/app.js pagehide + sendBeacon
    ("Abandoned", False,
     "Нет фонового планировщика таймаутов в рантайме экспортёра."),
    ("NavigationCompleted", False,
     "Навигация терминала не мапируется однозначно на UI-события текущей версии."),
    ("DeliveryFailed", False,
     "Приём синхронный (POST /api/track), очереди доставки нет — событие нечем эмитить."),
    ("RetryScheduled", False,
     "Ретраев нет (синхронный приём)."),
    ("RateLimited", True, None),            # экспортёр при 429 на /api/track
    ("TrafficError", False,
     "Конвейер фабрики (fetch_data.py и др.) не инструментирован эмиссией ошибок."),
    ("ExporterHealth", False,
     "Нет периодического self-check-планировщика; роль выполняют /watchtower/health|readyz."),
    ("DataGapDetected", True, None),        # gap detector экспортёра
    ("DataGapHealed", True, None),          # закрытие разрыва реальным backfill'ом
    ("BotFlagged", False,
     "Нет классификатора ботов; маркировка статическая (factory_pipeline=bot)."),
    ("AnomalyDetected", False,
     "Нет статистического детектора аномалий."),
    ("AbuseBlocked", False,
     "Нет blocking-слоя модерации трафика."),
    ("ConfigUpdated", False,
     "Покрывается гранулярными событиями реконсилёра; отдельного эмиттера нет."),
    ("EmergencyPause", False,
     "Механизма аварийной паузы генерации не существует."),
]
IMPLEMENTED_EVENTS = [e for e, impl, _ in EVENTS_CATALOG if impl]
UNAVAILABLE_EVENTS = [{"eventType": e, "reason": r} for e, impl, r in EVENTS_CATALOG if not impl]
CATALOG_EVENT_TYPES = {e for e, _, _ in EVENTS_CATALOG}
FUNNEL_STAGES = ["CampaignStarted", "SessionStarted", "PageView", "CTAClicked", "LandingReached"]
FUNNEL_UNAVAILABLE_STAGES = {e for e, impl, _ in EVENTS_CATALOG if not impl}

SOURCE_SYSTEM_IDS = [
    "x_twitter", "perplexity_ai", "chatgpt_search", "google_search",
    "short_video", "tiplink_referral", "direct_web", "factory_pipeline",
]
TARGET_PAGE_IDS = [
    "target_terminal", "target_sixsec", "target_duel",
    "target_crash", "target_quest", "target_tiplink_claim",
]
# Легаси-алиасы pageId -> канонический pageId (нормализация новых событий, не перезапись истории)
PAGE_ID_ALIASES = {"terminal": "target_terminal"}
VALID_SOURCE_TYPES = {"real", "bot", "hybrid"}

# ---------------------------------------------------------------------------
# PII SCRUBBING
# ---------------------------------------------------------------------------
# Девиация от исходного ТЗ (зафиксирована в отчёте): голый ключ "name" НЕ вычищается —
# в payload легитимно лежат публичные имена пулов/игр ("SOL/USDC"); персональные
# name-ключи перечислены явно. Адреса пулов (on-chain, публичные) PII не являются.
_FORBIDDEN_KEYS_RAW = {
    "ip", "ipaddress", "ip_address", "user_ip", "client_ip", "remote_addr",
    "x_forwarded_for", "xforwardedfor", "xff", "forwarded",
    "email", "e_mail", "mail", "phone", "telephone", "msisdn",
    "fingerprint", "deviceid", "device_id", "device", "machineid",
    "useragent", "user_agent", "ua",
    "cookie", "cookies", "setcookie", "set_cookie",
    "auth", "authorization", "auth_token", "token", "access_token", "refresh_token",
    "secret", "password", "passwd", "api_key", "apikey",
    "private_key", "privatekey", "secret_key", "secretkey",
    "seed_phrase", "seedphrase", "mnemonic", "recovery_phrase",
    "wallet", "walletaddress", "wallet_address", "walletpubkey",
    "signer", "signature",
    "latitude", "longitude", "geo", "geolocation", "location",
    "firstname", "first_name", "lastname", "last_name", "fullname", "full_name",
    "username", "user_name", "login", "screenname",
}
RE_IP4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# IPv6: сжатая форма (обязателен "::") ИЛИ полная (>=4 двоеточий) — чтобы не
# зацепить ложно строки вида "12:30:45"
RE_IP6 = re.compile(
    r"\b[0-9a-fA-F]{1,4}::(?:[0-9a-fA-F]{1,4}:?){0,6}\b"
    r"|\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b")
RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
RE_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")  # приватный ключ/подпись ed25519-вида
RE_WALLETLIKE_KEY = re.compile(r"wallet|signer|private|seed|mnemonic|signature", re.I)
SENSITIVE_QS = {
    "token", "sig", "signature", "email", "phone", "key", "wallet",
    "fbclid", "gclid", "dclid", "yclid", "session", "sid", "auth", "secret", "password",
}
_FORBIDDEN_NORM = {k.replace("_", "") for k in _FORBIDDEN_KEYS_RAW}


def _is_sensitive_key(key):
    norm = re.sub(r"[_\-]", "", str(key)).lower()
    return norm in _FORBIDDEN_NORM or bool(RE_WALLETLIKE_KEY.search(str(key)))


def _scrub_url(value):
    """Удалить чувствительные query-параметры из URL-строки (utm_* сохраняются)."""
    if "?" not in value:
        return value
    base, _, query = value.partition("?")
    kept = [p for p in query.split("&")
            if p.split("=", 1)[0].lower() not in SENSITIVE_QS]
    return base + ("?" + "&".join(kept) if kept else "")


def _scrub_string(value):
    v = RE_EMAIL.sub("[redacted]", value)
    v = RE_IP4.sub("[redacted]", v)
    v = RE_IP6.sub("[redacted]", v)
    v = RE_HEX64.sub("[redacted]", v)
    if v.startswith(("http://", "https://")) or "?" in v:
        v = _scrub_url(v)
    return v


def strip_pii(data):
    """Рекурсивно (dict / list / JSON-строки) очистить PII ДО записи в БД и логирования."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if _is_sensitive_key(k):
                continue  # запрещённый ключ удаляется целиком
            out[k] = strip_pii(v)
        return out
    if isinstance(data, list):
        return [strip_pii(x) for x in data]
    if isinstance(data, str):
        s = data.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.dumps(strip_pii(json.loads(s)), ensure_ascii=False)
            except Exception:
                pass
        return _scrub_string(data)
    return data


# ---------------------------------------------------------------------------
# ВРЕМЯ / ВАЛИДАЦИЯ
# ---------------------------------------------------------------------------
def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


RE_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class EventRejected(ValueError):
    """Событие нарушает схему. reason: schema | invalid_timestamp | non_utc_timestamp."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class InvalidCursor(ValueError):
    pass


def parse_ts_utc(value):
    """Строгий парс ISO 8601 -> нормализованная строка UTC с миллисекундами."""
    if not isinstance(value, str) or not RE_TS.match(value.strip()):
        raise EventRejected("invalid_timestamp", "timestamp must be ISO 8601 with timezone")
    raw = value.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise EventRejected("invalid_timestamp", "timestamp is not a valid datetime")
    if dt.utcoffset() != timedelta(0):
        raise EventRejected("non_utc_timestamp", "timestamp must be UTC (Z or +00:00)")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _lenient_ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def validate_raw_event(raw):
    """Валидация сырого события ДО канонизации. Бросает EventRejected."""
    if not isinstance(raw, dict):
        raise EventRejected("schema", "event must be an object")
    et = raw.get("eventType")
    if not isinstance(et, str) or not et.strip():
        raise EventRejected("schema", "eventType is required")
    try:
        seq = int(raw.get("seq") if raw.get("seq") is not None else 1)
    except (TypeError, ValueError):
        raise EventRejected("schema", "seq must be an integer")
    if seq < 1:
        raise EventRejected("schema", "seq must be >= 1")
    st = raw.get("sourceType")
    if st is not None and st not in VALID_SOURCE_TYPES:
        raise EventRejected("schema", f"unknown sourceType: {st!r}")
    if raw.get("timestamp") is not None:
        parse_ts_utc(raw["timestamp"])
    if raw.get("observedAt") is not None:
        parse_ts_utc(raw["observedAt"])
    return True


def _normalize_page_id(pid):
    if not isinstance(pid, str) or not pid:
        return "target_terminal"
    return PAGE_ID_ALIASES.get(pid, pid)


def _coerce_source_type(cleaned):
    """factory_pipeline ВСЕГДА bot; isBot -> bot; иначе по входу (валидированному)."""
    st = cleaned.get("sourceType")
    if cleaned.get("sourceId") == "factory_pipeline" or cleaned.get("isBot"):
        return "bot"
    if st in VALID_SOURCE_TYPES:
        return st
    return "real"


def canonicalize_event(raw):
    """Преобразует валидированное событие в канонический конверт Watchtower.

    sessionId: форма `sess_<...>` обязательна по контракту. Несоответствующий
    (но непустой) sessionId детерминированно псевдонимизируется через sha256
    (replay-дедуп сохраняется), а identity пересобирается из компонентов,
    чтобы в хранилище не попал исходный идентификатор.
    """
    cleaned = strip_pii(raw)
    cid = cleaned.get("campaignId") or "talkchart_interactive_radar"
    pid = _normalize_page_id(cleaned.get("pageId"))
    sid_raw = cleaned.get("sessionId")
    if isinstance(sid_raw, str) and sid_raw.startswith("sess_"):
        sid, sid_sanitized = sid_raw, False
    elif sid_raw:
        sid = f"sess_{hashlib.sha256(str(sid_raw).encode()).hexdigest()[:10]}"
        sid_sanitized = True
    else:
        sid, sid_sanitized = f"sess_{uuid.uuid4().hex[:10]}", False
    seq = int(cleaned.get("seq") or 1)
    et = cleaned.get("eventType") or "PageView"
    eid = cleaned.get("eventId") or f"ev_{uuid.uuid4().hex[:12]}"
    identity_raw = cleaned.get("identity")
    if identity_raw and not sid_sanitized:
        identity = identity_raw
    else:
        identity = f"offchain:trafficgen:{cid}:{pid}:{sid}:{seq}"
    unknown_type = et not in CATALOG_EVENT_TYPES

    return {
        "eventId": eid,
        "identity": identity,
        "chain": "offchain",
        "source": APP_ID,
        "app": APP_ID,
        "eventType": et,
        "timestamp": parse_ts_utc(cleaned["timestamp"]) if cleaned.get("timestamp") else now_utc_iso(),
        "observedAt": parse_ts_utc(cleaned["observedAt"]) if cleaned.get("observedAt") else now_utc_iso(),
        "campaignId": cid,
        "sourceId": cleaned.get("sourceId") or "direct_web",
        "sourceType": _coerce_source_type(cleaned),
        "pageId": pid,
        "sessionId": sid,
        "seq": seq,
        "payload": cleaned.get("payload") if isinstance(cleaned.get("payload"), dict) else {},
        "parserVersion": PARSER_VERSION,
        "dataQuality": "partial" if unknown_type else "complete",
    }


# ---------------------------------------------------------------------------
# КАТАЛОГИ: КАМПАНИИ, ИСТОЧНИКИ, СТРАНИЦЫ
# ---------------------------------------------------------------------------
CAMPAIGNS_DEF = [
    {
        "id": "talkchart_seo",
        "name": "Программный SEO и GEO поиск",
        "status": "active",
        "type": "organic_search",
        "targetAudience": "Crypto traders, meme coin researchers, AI answer engines",
        "landingPages": ["/pools/*.html", "/gainers/latest.html", "/vs/latest.html"],
        "trafficTypes": ["real", "bot"],
        "startedAt": "2026-09-21T00:00:00.000Z",
    },
    {
        "id": "talkchart_social_x",
        "name": "Дайджесты и Solana Blinks в X/Twitter",
        "status": "active",
        "type": "social_distribution",
        "targetAudience": "Solana degen community, Twitter crypto followers",
        "landingPages": ["/index.html", "/pools/*.html"],
        "trafficTypes": ["real"],
        "startedAt": "2026-09-21T00:00:00.000Z",
    },
    {
        "id": "talkchart_video_reels",
        "name": "15-секундные вертикальные видео (Shorts/TikTok/Reels)",
        "status": "active",
        "type": "short_video",
        "targetAudience": "TikTok & YouTube Shorts casual crypto viewers",
        "landingPages": ["/index.html"],
        "trafficTypes": ["real"],
        "startedAt": "2026-09-21T00:00:00.000Z",
    },
    {
        "id": "talkchart_interactive_radar",
        "name": "Терминал: китовый радар и бумажные прогнозы свечи 1ч",
        "status": "active",
        "type": "retention_loop",
        "targetAudience": "Active web terminal traders",
        "landingPages": ["/index.html"],
        "trafficTypes": ["real"],
        "startedAt": "2026-09-21T00:00:00.000Z",
    },
    {
        "id": "tiplink_welcome_drop",
        "name": "TipLink Onboarding (вход через Google без сид-фраз)",
        "status": "active",
        "type": "game_onboarding_funnel",
        "targetAudience": "Casual Web2/Web3 gamers converting into studio games",
        "landingPages": ["/index.html#games", "https://tiplink.io/campaign/talkchart-starter"],
        "trafficTypes": ["real"],
        "startedAt": "2026-09-21T00:00:00.000Z",
    },
]

SOURCES_DEF = [
    {"id": "x_twitter", "name": "X (Twitter) Feed & Dialect Blinks", "channel": "social", "sourceType": "real", "status": "connected"},
    {"id": "perplexity_ai", "name": "Perplexity AI Answer Engine", "channel": "geo_ai", "sourceType": "real", "status": "connected"},
    {"id": "chatgpt_search", "name": "ChatGPT Search / OpenAI", "channel": "geo_ai", "sourceType": "real", "status": "connected"},
    {"id": "google_search", "name": "Google Organic Search", "channel": "seo", "sourceType": "real", "status": "connected"},
    {"id": "short_video", "name": "Short Video Feeds (TikTok / YouTube Shorts)", "channel": "video", "sourceType": "real", "status": "connected"},
    {"id": "tiplink_referral", "name": "TipLink Google Onboarding Link", "channel": "referral", "sourceType": "real", "status": "connected"},
    {"id": "direct_web", "name": "Direct Web Browser Access", "channel": "direct", "sourceType": "real", "status": "connected"},
    {"id": "factory_pipeline", "name": "GitHub Actions Content Factory (Automated Crawler)", "channel": "internal_ci", "sourceType": "bot", "status": "connected"},
]


def get_target_pages_def():
    pages = [
        {"id": "target_terminal", "name": "TalkChart Live Terminal", "url": f"{config.SITE_URL}/index.html", "role": "acquisition_hub", "category": "utility", "campaignId": "talkchart_interactive_radar"},
        {"id": "target_sixsec", "name": "SixSec", "url": "https://example.com/game1", "role": "studio_game", "category": "game_1", "campaignId": "tiplink_welcome_drop"},
        {"id": "target_duel", "name": "CandleDuel", "url": "https://example.com/game2", "role": "studio_game", "category": "game_2", "campaignId": "tiplink_welcome_drop"},
        {"id": "target_crash", "name": "MemeCrash", "url": "https://example.com/game3", "role": "studio_game", "category": "game_3", "campaignId": "tiplink_welcome_drop"},
        {"id": "target_quest", "name": "WhaleQuest", "url": "https://example.com/game4", "role": "studio_game", "category": "game_4", "campaignId": "tiplink_welcome_drop"},
        {"id": "target_tiplink_claim", "name": "TipLink Starter Pass Claim", "url": "https://tiplink.io/campaign/talkchart-starter", "role": "onboarding_bridge", "category": "voucher", "campaignId": "tiplink_welcome_drop"},
    ]
    games_js_path = os.path.join(config.SITE_DIR, "games.js")
    if os.path.exists(games_js_path):
        try:
            with open(games_js_path, "r", encoding="utf-8") as f:
                content = f.read()
            urls = re.findall(r'url:\s*"(https?://[^"]+)"', content)
            names = re.findall(r'name:\s*"([^"]+)"', content)
            for idx, u in enumerate(urls):
                if idx < 4:
                    pages[idx + 1]["url"] = u
                    if idx < len(names):
                        pages[idx + 1]["name"] = names[idx]
        except Exception:
            pass
    return pages


def wrap_envelope(data, quality="complete", confidence=1.0, period="7d UTC"):
    return {
        "data": data,
        "generatedAt": now_utc_iso(),
        "period": period,
        "source": SOURCE_EXPORTER,
        "dataQuality": quality,
        "confidence": confidence,
        "parserVersion": PARSER_VERSION,
    }


def error_envelope(message, code, period="snapshot"):
    return wrap_envelope({"error": message, "code": code},
                         quality="unavailable", confidence=0.0, period=period)


# ---------------------------------------------------------------------------
# ХРАНИЛИЩЕ (SQLite ACID + Dedup + Gap Detection/Healing + persist метрик)
# ---------------------------------------------------------------------------
def _default_metrics():
    return {
        # производные из events (пересчитываются при старте):
        "events_total": {"real": 0, "bot": 0, "hybrid": 0},
        "data_gaps_total": 0,
        "data_gaps_healed_total": 0,
        # не выводимые из events (персистятся в metrics_state):
        "events_duplicate_total": 0,
        "events_rejected_total": {"schema": 0, "pii": 0, "unknown_type": 0},
        "events_unknown_type": {},
        "delivery_failures_total": 0,
        "exporter_errors_total": 0,
        "rate_limited_total": 0,
    }


class EventStore:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._mlock = threading.Lock()
        self.metrics = _default_metrics()
        self.init_db()
        self.load_initial_metrics()

    def get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def get_connection(self):
        return self.get_conn()

    # --- совместимость со старыми тестами/интеграциями ----------------------
    def ingest_event(self, raw):
        res = self.record_event(raw)
        return {
            "success": res.get("status") in ("accepted", "duplicate"),
            "duplicate": res.get("status") == "duplicate",
            "rejected": res.get("status") == "rejected",
            "reason": res.get("reason"),
            "message": res.get("message"),
            "eventId": res.get("eventId"),
            "identity": res.get("identity"),
            "id": res.get("id"),
        }

    # --- схема ---------------------------------------------------------------
    def init_db(self):
        with self.get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE,
                    identity TEXT UNIQUE,
                    chain TEXT, source TEXT, app TEXT,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    campaign_id TEXT, source_id TEXT, source_type TEXT,
                    page_id TEXT, session_id TEXT,
                    seq INTEGER,
                    payload TEXT,
                    parser_version TEXT,
                    data_quality TEXT,
                    is_synthetic INTEGER DEFAULT 0
                );
            """)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
            if "is_synthetic" not in cols:
                conn.execute("ALTER TABLE events ADD COLUMN is_synthetic INTEGER DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_sequences (
                    session_id TEXT PRIMARY KEY,
                    last_seq INTEGER,
                    updated_at TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_gaps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    from_seq INTEGER NOT NULL,
                    to_seq INTEGER NOT NULL,
                    identity TEXT UNIQUE,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    healed_at TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gaps_session ON session_gaps(session_id, status);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS catalog_state (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    def_hash TEXT,
                    status TEXT,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    PRIMARY KEY (kind, id)
                );
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS daily_snapshots (date TEXT PRIMARY KEY, data TEXT, generated_at TEXT);")
            conn.execute("CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, name TEXT, type TEXT, status TEXT, data TEXT);")
            conn.execute("CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, name TEXT, channel TEXT, source_type TEXT, status TEXT);")
            conn.execute("CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, name TEXT, url TEXT, role TEXT, category TEXT);")
            conn.execute("CREATE TABLE IF NOT EXISTS aggregates_daily (date TEXT PRIMARY KEY, metrics TEXT, generated_at TEXT);")
            conn.execute("CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, alert_type TEXT, severity TEXT, status TEXT, data TEXT, created_at TEXT, resolved_at TEXT);")
            conn.execute("CREATE TABLE IF NOT EXISTS sync_cursors (source TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT);")

    # --- метрики: производные из events + персистентные счётчики -------------
    def load_initial_metrics(self):
        try:
            with self.get_conn() as conn:
                for r in conn.execute("SELECT source_type, COUNT(*) AS cnt FROM events GROUP BY source_type").fetchall():
                    st = r["source_type"] if r["source_type"] in ("real", "bot", "hybrid") else "real"
                    self.metrics["events_total"][st] += r["cnt"]
                row = conn.execute("SELECT COUNT(*) AS cnt FROM events WHERE event_type='DataGapDetected'").fetchone()
                self.metrics["data_gaps_total"] = row["cnt"] if row else 0
                row = conn.execute("SELECT COUNT(*) AS cnt FROM events WHERE event_type='DataGapHealed'").fetchone()
                self.metrics["data_gaps_healed_total"] = row["cnt"] if row else 0
                persisted = conn.execute("SELECT value FROM metrics_state WHERE key='metrics'").fetchone()
            if persisted and persisted["value"]:
                saved = json.loads(persisted["value"])
                for k in ("events_duplicate_total", "delivery_failures_total",
                          "exporter_errors_total", "rate_limited_total"):
                    if isinstance(saved.get(k), int):
                        self.metrics[k] = saved[k]
                for k in ("events_rejected_total", "events_unknown_type"):
                    if isinstance(saved.get(k), dict):
                        self.metrics[k].update(saved[k])
        except Exception:
            pass

    def _persist_metrics(self):
        payload = json.dumps({
            "events_duplicate_total": self.metrics["events_duplicate_total"],
            "events_rejected_total": self.metrics["events_rejected_total"],
            "events_unknown_type": self.metrics["events_unknown_type"],
            "delivery_failures_total": self.metrics["delivery_failures_total"],
            "exporter_errors_total": self.metrics["exporter_errors_total"],
            "rate_limited_total": self.metrics["rate_limited_total"],
        }, ensure_ascii=False)
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO metrics_state (key, value) VALUES ('metrics', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (payload,))

    def bump_metric(self, *path, amount=1):
        """Инкремент счётчика + write-through персист. ВЫЗЫВАТЬ ТОЛЬКО ВНЕ write-транзакций."""
        with self._mlock:
            node = self.metrics
            for key in path[:-1]:
                node = node.setdefault(key, {})
            last = path[-1]
            node[last] = (node.get(last, 0) if isinstance(node.get(last, 0), int) else 0) + amount
            try:
                self._persist_metrics()
            except Exception:
                pass

    def bump_unknown_type(self, event_type):
        with self._mlock:
            known = self.metrics["events_unknown_type"]
            label = event_type if (len(known) < KNOWN_EVENT_TYPE_LABEL_CAP or event_type in known) else "__other__"
            known[label] = known.get(label, 0) + 1
            try:
                self._persist_metrics()
            except Exception:
                pass

    # --- приём событий --------------------------------------------------------
    def record_event(self, raw):
        """Pipeline: strip_pii -> validate -> canonicalize -> dedupe -> gap/heal -> store."""
        try:
            raw_pii = strip_pii(raw if isinstance(raw, dict) else {})
            validate_raw_event(raw_pii)
        except EventRejected as e:
            reason = e.reason if e.reason in ("schema", "pii") else "schema"
            self.bump_metric("events_rejected_total", reason)
            return {"status": "rejected", "reason": e.reason, "message": str(e),
                    "eventId": raw.get("eventId") if isinstance(raw, dict) else None,
                    "identity": None, "id": None}

        ev = canonicalize_event(raw_pii)
        unknown_type = ev["eventType"] not in CATALOG_EVENT_TYPES
        if unknown_type:
            self.bump_metric("events_rejected_total", "unknown_type")
            self.bump_unknown_type(ev["eventType"])

        is_synth = 1 if ev["payload"].get("synthetic") is True else 0
        payload_str = json.dumps(ev["payload"], ensure_ascii=False)
        system_session = str(ev["sessionId"]).startswith("sess_system")

        dup_row_id = None
        row_id = None
        gap_opened = False
        healed = 0

        with self.get_conn() as conn:
            # 1. Дедупликация по eventId ИЛИ identity
            existing = conn.execute(
                "SELECT id FROM events WHERE event_id = ? OR identity = ?",
                (ev["eventId"], ev["identity"])).fetchone()
            if existing:
                dup_row_id = existing["id"]
            else:
                # 2. Gap detection по seq (только пользовательские сессии)
                if not system_session:
                    seq_row = conn.execute(
                        "SELECT last_seq FROM session_sequences WHERE session_id = ?",
                        (ev["sessionId"],)).fetchone()
                    if seq_row is not None:
                        last_seq = seq_row["last_seq"]
                        if ev["seq"] > last_seq + 1:
                            self._open_gap(conn, ev, last_seq + 1, ev["seq"] - 1)
                            gap_opened = True
                        if ev["seq"] >= last_seq + 1:
                            conn.execute(
                                "UPDATE session_sequences SET last_seq = ?, updated_at = ? WHERE session_id = ?",
                                (ev["seq"], ev["observedAt"], ev["sessionId"]))
                        # seq <= last_seq: позднее/backfill-событие — last_seq не двигаем
                    else:
                        conn.execute(
                            "INSERT INTO session_sequences (session_id, last_seq, updated_at) VALUES (?, ?, ?)",
                            (ev["sessionId"], ev["seq"], ev["observedAt"]))

                # 3. Запись события
                cur = conn.execute("""
                    INSERT INTO events
                    (event_id, identity, chain, source, app, event_type, timestamp, observed_at,
                     campaign_id, source_id, source_type, page_id, session_id, seq,
                     payload, parser_version, data_quality, is_synthetic)
                    VALUES (?, ?, 'offchain', 'trafficgen', 'trafficgen', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ev["eventId"], ev["identity"], ev["eventType"], ev["timestamp"], ev["observedAt"],
                    ev["campaignId"], ev["sourceId"], ev["sourceType"], ev["pageId"], ev["sessionId"],
                    ev["seq"], payload_str, PARSER_VERSION, ev["dataQuality"], is_synth,
                ))
                row_id = cur.lastrowid

                # 4. Healing: может ли это событие закрыть открытые разрывы сессии?
                if not system_session:
                    healed = self._try_heal_gaps(conn, ev)
                conn.commit()
        # --- транзакция закрыта; метрики обновляем только здесь ----------------
        if dup_row_id is not None:
            self.bump_metric("events_duplicate_total")
            return {"status": "duplicate", "eventId": ev["eventId"],
                    "identity": ev["identity"], "id": dup_row_id}

        st = ev["sourceType"] if ev["sourceType"] in ("real", "bot", "hybrid") else "real"
        self.bump_metric("events_total", st)
        if gap_opened:
            self.bump_metric("data_gaps_total")
        if healed:
            self.bump_metric("data_gaps_healed_total", amount=healed)

        return {"status": "accepted", "eventId": ev["eventId"],
                "identity": ev["identity"], "id": row_id}

    def _open_gap(self, conn, ev, from_seq, to_seq):
        """Открыть разрыв: событие DataGapDetected + строка session_gaps(status='open')."""
        missing = to_seq - from_seq + 1
        gap_identity = (f"offchain:trafficgen:{ev['campaignId']}:{ev['pageId']}:"
                        f"{ev['sessionId']}:gap:{from_seq}-{to_seq}")
        conn.execute("""
            INSERT OR IGNORE INTO session_gaps (session_id, from_seq, to_seq, identity, status, created_at)
            VALUES (?, ?, ?, ?, 'open', ?)
        """, (ev["sessionId"], from_seq, to_seq, gap_identity, now_utc_iso()))
        conn.execute("""
            INSERT OR IGNORE INTO events
            (event_id, identity, chain, source, app, event_type, timestamp, observed_at,
             campaign_id, source_id, source_type, page_id, session_id, seq,
             payload, parser_version, data_quality, is_synthetic)
            VALUES (?, ?, 'offchain', 'trafficgen', 'trafficgen', 'DataGapDetected', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', 0)
        """, (
            f"ev_{uuid.uuid4().hex[:12]}", gap_identity, ev["timestamp"], ev["observedAt"],
            ev["campaignId"], ev["sourceId"], ev["sourceType"], ev["pageId"], ev["sessionId"],
            from_seq,
            json.dumps({"expectedSeq": from_seq, "receivedSeq": to_seq + 1,
                        "missingCount": missing}, ensure_ascii=False),
            PARSER_VERSION,
        ))

    def _try_heal_gaps(self, conn, ev):
        """Закрыть открытые разрывы сессии, если весь диапазон seq реально присутствует."""
        healed = 0
        open_gaps = conn.execute(
            "SELECT * FROM session_gaps WHERE session_id = ? AND status = 'open'",
            (ev["sessionId"],)).fetchall()
        for gap in open_gaps:
            expected_count = gap["to_seq"] - gap["from_seq"] + 1
            covered = conn.execute("""
                SELECT COUNT(DISTINCT seq) AS cnt FROM events
                WHERE session_id = ? AND seq BETWEEN ? AND ?
                  AND event_type NOT IN ('DataGapDetected', 'DataGapHealed')
            """, (ev["sessionId"], gap["from_seq"], gap["to_seq"])).fetchone()["cnt"]
            if covered < expected_count:
                continue  # частичное заполнение — разрыв остаётся open (промт 7.3 п.5)
            heal_identity = (f"offchain:trafficgen:{ev['campaignId']}:{ev['pageId']}:"
                             f"{ev['sessionId']}:heal:{gap['from_seq']}-{gap['to_seq']}")
            healed_seqs = list(range(gap["from_seq"], gap["to_seq"] + 1))
            conn.execute("""
                INSERT OR IGNORE INTO events
                (event_id, identity, chain, source, app, event_type, timestamp, observed_at,
                 campaign_id, source_id, source_type, page_id, session_id, seq,
                 payload, parser_version, data_quality, is_synthetic)
                VALUES (?, ?, 'offchain', 'trafficgen', 'trafficgen', 'DataGapHealed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', 0)
            """, (
                f"ev_{uuid.uuid4().hex[:12]}", heal_identity, now_utc_iso(), now_utc_iso(),
                ev["campaignId"], ev["sourceId"], ev["sourceType"], ev["pageId"],
                ev["sessionId"], gap["from_seq"],
                json.dumps({"healedSeq": healed_seqs, "gapRef": gap["identity"],
                            "healedAt": now_utc_iso()}, ensure_ascii=False),
                PARSER_VERSION,
            ))
            conn.execute("UPDATE session_gaps SET status = 'healed', healed_at = ? WHERE id = ?",
                         (now_utc_iso(), gap["id"]))
            healed += 1
        return healed

    # --- чтение ----------------------------------------------------------------
    def get_events(self, cursor=None, limit=50, filters=None):
        filters = filters or {}
        limit = max(1, min(int(limit), 500))
        last_id = 0
        if cursor:
            try:
                decoded = base64.b64decode(cursor.encode(), validate=True).decode()
            except Exception:
                raise InvalidCursor("cursor is not valid base64")
            m = re.fullmatch(r"cursor:(\d+)", decoded)
            if m:
                last_id = int(m.group(1))
            elif re.fullmatch(r"\d+", decoded):
                # допустимая клиентская форма replay: base64 от голого id
                # (пример: MA== -> "0" -> чтение с начала). Каноничная форма — 'cursor:<id>'.
                last_id = int(decoded)
            else:
                raise InvalidCursor("cursor payload must match 'cursor:<id>' (or bare integer id)")

        query = ["SELECT * FROM events WHERE id > ?"]
        params = [last_id]
        if filters.get("eventType"):
            query.append("AND event_type = ?")
            params.append(filters["eventType"])
        if filters.get("campaignId"):
            query.append("AND campaign_id = ?")
            params.append(filters["campaignId"])
        if filters.get("sourceType"):
            query.append("AND source_type = ?")
            params.append(filters["sourceType"])
        if filters.get("since"):
            query.append("AND timestamp >= ?")
            params.append(filters["since"])
        query.append("ORDER BY id ASC LIMIT ?")
        params.append(limit + 1)

        with self.get_conn() as conn:
            rows = conn.execute(" ".join(query), params).fetchall()
            rows = [dict(r) for r in rows]

        has_more = len(rows) > limit
        result_rows = rows[:limit]
        events = []
        for r in result_rows:
            events.append({
                "eventId": r["event_id"], "identity": r["identity"], "chain": r["chain"],
                "source": r["source"], "app": r["app"], "eventType": r["event_type"],
                "timestamp": r["timestamp"], "observedAt": r["observed_at"],
                "campaignId": r["campaign_id"], "sourceId": r["source_id"],
                "sourceType": r["source_type"], "pageId": r["page_id"],
                "sessionId": r["session_id"], "seq": r["seq"],
                "payload": json.loads(r["payload"] or "{}"),
                "parserVersion": r["parser_version"], "dataQuality": r["data_quality"],
            })
        next_cursor = None
        if has_more and result_rows:
            next_cursor = base64.b64encode(f"cursor:{result_rows[-1]['id']}".encode()).decode()
        return events, next_cursor

    def get_alerts(self):
        """Алерты: разрывы (open=active, healed=resolved) + legacy-события DataGapDetected."""
        alerts = []
        seen_identities = set()
        with self.get_conn() as conn:
            for g in conn.execute("SELECT * FROM session_gaps ORDER BY id DESC LIMIT 100").fetchall():
                seen_identities.add(g["identity"])
                alerts.append({
                    "id": f"gap_{g['id']}",
                    "alertId": f"gap_{g['id']}",
                    "alertType": "DataGapDetected",
                    "severity": "warning",
                    "status": "active" if g["status"] == "open" else "resolved",
                    "sessionId": g["session_id"],
                    "campaignId": None,
                    "detectedAt": g["created_at"],
                    "timestamp": g["created_at"],
                    "resolvedAt": g["healed_at"],
                    "details": {
                        "expectedSeq": g["from_seq"],
                        "receivedSeq": g["to_seq"] + 1,
                        "missingCount": g["to_seq"] - g["from_seq"] + 1,
                    },
                })
            legacy = conn.execute(
                "SELECT * FROM events WHERE event_type = 'DataGapDetected' ORDER BY id DESC LIMIT 50"
            ).fetchall()
            for r in legacy:
                if r["identity"] in seen_identities:
                    continue
                alerts.append({
                    "id": f"gap_legacy_{r['id']}",
                    "alertId": f"gap_legacy_{r['id']}",
                    "alertType": "DataGapDetected",
                    "severity": "warning",
                    "status": "active",
                    "sessionId": r["session_id"],
                    "campaignId": r["campaign_id"],
                    "detectedAt": r["timestamp"],
                    "timestamp": r["timestamp"],
                    "resolvedAt": None,
                    "details": json.loads(r["payload"] or "{}"),
                })
        return alerts

    def get_funnels(self, period_days=7):
        return [compute_funnel(self, period_days=period_days)]

    def get_config(self):
        return {
            "appId": APP_ID,
            "displayName": "TalkChart Traffic Generator & Audience Layer",
            "kind": "traffic-generator",
            "stage": "live",
            "deploymentUrl": config.SITE_URL,
            "techStack": "python3, vanilla-js, github-actions, sqlite3",
            "trafficType": "hybrid",
            "campaignStore": "config",
            "eventStore": "database",
            "eventRetention": f"{EVENT_RETENTION_DAYS}d",
            "auth": "api-key" if _watchtower_tokens() else "none",
            "timeReference": "utc",
            "parserVersion": PARSER_VERSION,
            "readOnly": True,
            "campaigns": CAMPAIGNS_DEF,
            "implementedEvents": IMPLEMENTED_EVENTS,
            "unavailableEvents": [u["eventType"] for u in UNAVAILABLE_EVENTS],
            "unavailableEventsWithReasons": UNAVAILABLE_EVENTS,
            "sourceSystems": SOURCE_SYSTEM_IDS,
            "targetPages": TARGET_PAGE_IDS,
            "bufferNote": "Очереди/буфера нет по архитектуре (синхронный приём в SQLite); "
                          "trafficgen_buffer_depth = 0 — константа по проекту, не заглушка.",
            "sessionDurationNote": "SessionEnded эмитится клиентом (pagehide); до его накопления "
                                   "длительность — оценка first->last event (estimate).",
        }

    # --- retention -------------------------------------------------------------
    def prune_retention(self, event_days=None, aggregate_days=None):
        """Retention-прайнинг: события старше 30d, агрегаты старше 180d."""
        event_days = event_days if event_days is not None else EVENT_RETENTION_DAYS
        aggregate_days = aggregate_days if aggregate_days is not None else AGGREGATE_RETENTION_DAYS
        event_cutoff = (datetime.now(timezone.utc) - timedelta(days=event_days)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        agg_cutoff = (datetime.now(timezone.utc) - timedelta(days=aggregate_days)) \
            .strftime("%Y-%m-%d")
        out = {"events": 0, "gaps": 0, "aggregates": 0, "sessions": 0}
        with self.get_conn() as conn:
            cur = conn.execute("DELETE FROM events WHERE timestamp < ?", (event_cutoff,))
            out["events"] = cur.rowcount
            cur = conn.execute("DELETE FROM session_gaps WHERE created_at < ?", (event_cutoff,))
            out["gaps"] = cur.rowcount
            cur = conn.execute("DELETE FROM session_sequences WHERE updated_at < ?", (event_cutoff,))
            out["sessions"] = cur.rowcount
            cur = conn.execute("DELETE FROM aggregates_daily WHERE date < ?", (agg_cutoff,))
            out["aggregates"] = cur.rowcount
            conn.commit()
        return out


STORE = EventStore()
WatchtowerStore = EventStore  # alias совместимости


# ---------------------------------------------------------------------------
# РЕКОНСИЛЁР КАТАЛОГОВ (реальные lifecycle-сигналы из config store)
# ---------------------------------------------------------------------------
def _sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def reconcile_catalog(store=None):
    """Сверка config store с catalog_state; эмитит реальные lifecycle-события.

    Идемпотентно благодаря детерминированным identity: повторный запуск
    (и повторный вызов) не создаёт дублей. Три фазы (чтение -> эмиссия -> запись
    состояния), чтобы не держать write-транзакцию открытой поверх record_event.
    """
    s = store or STORE
    now = now_utc_iso()
    emitted = []

    # --- фаза 1: читаем текущее состояние и планируем действия ----------------
    plan = []  # (kind, id, event_type|None, session_suffix, payload, new_hash, new_status)

    def campaign_plan():
        seen = set()
        for c in CAMPAIGNS_DEF:
            cid = c["id"]
            seen.add(cid)
            dh, st = _sha(c), c.get("status", "active")
            row = state_rows.get(("campaign", cid))
            if row is None:
                plan.append(("campaign", cid, "CampaignCreated", ("created",),
                             {"name": c["name"], "type": c["type"],
                              "createdBy": "config_reconciler", "defHash": dh}, dh, st))
                if st == "active":
                    plan.append(("campaign", cid, "CampaignStarted", ("started",),
                                 {"startedAt": c.get("startedAt"),
                                  "channels": c.get("trafficTypes", [])}, dh, st))
            else:
                if row["def_hash"] != dh:
                    plan.append(("campaign", cid, "CampaignUpdated", (f"updated:{dh}",),
                                 {"changedFields": ["definition"],
                                  "previous": {"defHash": row["def_hash"]}, "defHash": dh}, dh, st))
                if row["status"] != st:
                    if st == "stopped":
                        plan.append(("campaign", cid, "CampaignStopped", (f"stopped:{dh[:8]}",),
                                     {"stoppedAt": now, "reason": "config status changed"}, dh, st))
                    elif st == "active":
                        plan.append(("campaign", cid, "CampaignStarted", (f"restarted:{dh[:8]}",),
                                     {"startedAt": now,
                                      "channels": c.get("trafficTypes", [])}, dh, st))
                if not plan or plan[-1][1] != cid or plan[-1][2] is None:
                    plan.append(("campaign", cid, None, (None,), {}, dh, st))
        for (kind, rid), row in state_rows.items():
            if kind == "campaign" and rid not in seen and row["status"] == "active":
                suffix = f"removed:{_sha({'id': rid, 'at': now})[:8]}"
                plan.append(("campaign", rid, "CampaignStopped", (suffix,),
                             {"stoppedAt": now, "reason": "removed from config store"},
                             row["def_hash"], "stopped"))

    def source_plan():
        for src in SOURCES_DEF:
            sid = src["id"]
            dh, st = _sha(src), src.get("status", "connected")
            row = state_rows.get(("source", sid))
            if row is None:
                plan.append(("source", sid, "SourceConnected", ("connected",),
                             {"sourceId": sid, "channel": src["channel"]}, dh, st))
            else:
                if row["status"] != st:
                    et = ("SourceConnected" if st == "connected"
                          else "SourceDisconnected" if st == "disconnected"
                          else "SourceHealthChanged")
                    plan.append(("source", sid, et, (f"health:{row['status']}-{st}",),
                                 {"sourceId": sid, "from": row["status"], "to": st,
                                  "check": "config_reconciler"}, dh, st))
                else:
                    plan.append(("source", sid, None, (None,), {}, dh, st))

    def page_plan():
        pages = get_target_pages_def()
        current = set()
        for p in pages:
            pid = p["id"]
            current.add(pid)
            dh = _sha(p)
            row = state_rows.get(("page", pid))
            if row is None:
                plan.append(("page", pid, "PageAssigned", (f"assigned:{dh}",),
                             {"pageId": pid, "campaignId": p.get("campaignId"),
                              "url": p["url"]}, dh, "assigned"))
            else:
                plan.append(("page", pid, None, (None,), {}, dh, "assigned"))
        for (kind, rid), row in state_rows.items():
            if kind == "page" and rid not in current and row["status"] != "removed":
                suffix = f"removed:{_sha({'id': rid, 'at': now})[:8]}"
                plan.append(("page", rid, "PageRemoved", (suffix,),
                             {"pageId": rid, "reason": "removed from config store"},
                             row["def_hash"], "removed"))

    with s.get_conn() as conn:
        state_rows = {
            (r["kind"], r["id"]): {"def_hash": r["def_hash"], "status": r["status"]}
            for r in conn.execute("SELECT kind, id, def_hash, status FROM catalog_state").fetchall()
        }
    campaign_plan()
    source_plan()
    page_plan()

    # --- фаза 2: эмиссия (identity детерминированы -> идемпотентно) -----------
    for kind, rid, et, suffix_parts, payload, dh, st in plan:
        if et is None:
            continue
        if kind == "campaign":
            sess = f"sess_system_camp_{rid}"
            campaign_id = rid
        elif kind == "source":
            sess = f"sess_system_src_{rid}"
            campaign_id = "system"
        else:
            sess = f"sess_system_page_{rid}"
            campaign_id = payload.get("campaignId") or "system"
        suffix = suffix_parts[0]
        identity = f"offchain:trafficgen:{campaign_id}:target_terminal:{sess}:{suffix}"
        res = s.record_event({
            "eventType": et,
            "campaignId": campaign_id,
            "sourceId": "factory_pipeline",
            "sourceType": "bot",
            "pageId": "target_terminal",
            "sessionId": sess,
            "seq": int(time.time() * 1000),
            "identity": identity,
            "payload": payload,
        })
        if res.get("status") == "accepted":
            emitted.append(et)

    # --- фаза 3: запись нового состояния каталога ----------------------------
    with s.get_conn() as conn:
        for kind, rid, et, suffix_parts, payload, dh, st in plan:
            conn.execute("""
                INSERT INTO catalog_state (kind, id, def_hash, status, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, id) DO UPDATE SET def_hash=excluded.def_hash,
                    status=excluded.status, last_seen_at=excluded.last_seen_at
            """, (kind, rid, dh, st, now, now))
        conn.commit()
    return emitted


# ---------------------------------------------------------------------------
# ДНЕВНЫЕ МЕТРИКИ (8.1) И ВОРОНКА (8.2)
# ---------------------------------------------------------------------------
def _percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, int(math.ceil(q / 100.0 * len(sorted_vals))) - 1))
    return round(sorted_vals[idx], 1)


def _session_stats(rows):
    """Статистика по выборке событий. Возвращает (stats, by_campaign, by_source, by_page)."""
    sessions = {}
    campaigns = {}
    sources = {}
    pages = {}
    types = {"real": 0, "bot": 0, "hybrid": 0}
    page_views = cta_clicks = landing_reached = 0

    for r in rows:
        sid = r["session_id"]
        et = r["event_type"]
        st = r["source_type"] if r["source_type"] in types else "real"
        cid = r["campaign_id"] or "-"
        src = r["source_id"] or "-"
        pid = r["page_id"] or "-"

        types[st] += 1
        if et == "PageView":
            page_views += 1
            pages.setdefault(pid, {"pageViews": 0, "ctaClicks": 0})["pageViews"] += 1
            campaigns.setdefault(cid, {"pageViews": 0, "sessions": set(), "ctaClicks": 0, "landingReached": 0})["pageViews"] += 1
            sources.setdefault(src, {"pageViews": 0, "sessions": set(), "sourceType": st})["pageViews"] += 1
        elif et == "CTAClicked":
            cta_clicks += 1
            pages.setdefault(pid, {"pageViews": 0, "ctaClicks": 0})["ctaClicks"] += 1
            campaigns.setdefault(cid, {"pageViews": 0, "sessions": set(), "ctaClicks": 0, "landingReached": 0})["ctaClicks"] += 1
        elif et == "LandingReached":
            landing_reached += 1
            campaigns.setdefault(cid, {"pageViews": 0, "sessions": set(), "ctaClicks": 0, "landingReached": 0})["landingReached"] += 1

        sessions.setdefault(sid, {"count": 0, "start": r["timestamp"], "end": r["timestamp"], "types": set()})
        s = sessions[sid]
        s["count"] += 1
        s["types"].add(st)
        if r["timestamp"] < s["start"]:
            s["start"] = r["timestamp"]
        if r["timestamp"] > s["end"]:
            s["end"] = r["timestamp"]
        campaigns.setdefault(cid, {"pageViews": 0, "sessions": set(), "ctaClicks": 0, "landingReached": 0})["sessions"].add(sid)
        sources.setdefault(src, {"pageViews": 0, "sessions": set(), "sourceType": st})["sessions"].add(sid)

    durations, bounces = [], 0
    visitors_by_type = {"real": 0, "bot": 0, "hybrid": 0}
    for s in sessions.values():
        if s["count"] == 1:
            bounces += 1
        st = "bot" if "bot" in s["types"] else ("hybrid" if "hybrid" in s["types"] else "real")
        visitors_by_type[st] += 1
        t0, t1 = _lenient_ts(s["start"]), _lenient_ts(s["end"])
        if t0 and t1:
            durations.append(max(0.0, (t1 - t0).total_seconds()))
    durations.sort()

    n_sessions = len(sessions)
    stats = {
        "pageViews": page_views,
        "sessions": n_sessions,
        "uniquePseudoVisitors": n_sessions,
        "visitorsByType": visitors_by_type,
        "sessionDurationSeconds": {
            "avg": round(sum(durations) / len(durations), 1) if durations else 0.0,
            "p50": _percentile(durations, 50),
            "p95": _percentile(durations, 95),
            "estimate": True,
        },
        "bounceRate": round(bounces / n_sessions, 3) if n_sessions else 0.0,
        "ctaClickRate": round(cta_clicks / page_views, 3) if page_views else 0.0,
        "landingReachedRate": round(landing_reached / cta_clicks, 3) if cta_clicks else 0.0,
        "trafficType": types,
    }
    by_campaign = {k: {"pageViews": v["pageViews"], "sessions": len(v["sessions"]),
                       "ctaClicks": v["ctaClicks"], "landingReached": v["landingReached"]}
                   for k, v in campaigns.items()}
    by_source = {k: {"pageViews": v["pageViews"], "sessions": len(v["sessions"]),
                     "sourceType": v["sourceType"]}
                 for k, v in sources.items()}
    return stats, by_campaign, by_source, pages


def compute_metrics(period_days=7, store=None):
    """metrics/daily по 8.1: непрерывная серия дней + totals + breakdowns + integrity."""
    s = store or STORE
    period_days = max(1, min(int(period_days), 90))
    today = datetime.now(timezone.utc).date()
    day_from = today - timedelta(days=period_days - 1)
    cutoff = datetime.combine(day_from, datetime.min.time(), tzinfo=timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")

    with s.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT * FROM events
            WHERE timestamp >= ? AND is_synthetic = 0
            ORDER BY id ASC
        """, (cutoff,)).fetchall()]

    by_day = {}
    for r in rows:
        by_day.setdefault((r["timestamp"] or "")[:10], []).append(r)

    days = []
    for i in range(period_days):
        d_iso = (day_from + timedelta(days=i)).isoformat()
        d_rows = by_day.get(d_iso, [])
        dstats, dc, ds_, dp = _session_stats(d_rows)
        days.append({
            "date": d_iso,
            **dstats,
            "events": {
                "total": len(d_rows),
                "byType": {et: sum(1 for r in d_rows if r["event_type"] == et)
                           for et in {r["event_type"] for r in d_rows}},
            },
            "errors": {
                "deliveryFailures": 0,   # счётчик процесс-глобальный, см. totals.errors
                "exporterErrors": 0,
                "trafficErrors": sum(1 for r in d_rows if r["event_type"] == "TrafficError"),
            },
            "integrity": {
                "duplicates": 0,         # процесс-глобальный счётчик, см. totals.integrity
                "rejected": 0,
                "dataGaps": sum(1 for r in d_rows if r["event_type"] == "DataGapDetected"),
                "dataGapsHealed": sum(1 for r in d_rows if r["event_type"] == "DataGapHealed"),
            },
            "breakdowns": {"byCampaign": dc, "bySource": ds_, "byPage": dp},
        })

    stats_total, tc, ts_, tp = _session_stats(rows)
    rejected_total = sum(s.metrics["events_rejected_total"].values())
    totals = {
        "periodDays": period_days,
        "totalEvents": len(rows),
        **stats_total,
        "campaignBreakdown": {k: v["pageViews"] for k, v in tc.items()},
        "sourceBreakdown": {k: v["pageViews"] for k, v in ts_.items()},
        "trafficTypeBreakdown": stats_total["trafficType"],
        "errors": {"deliveryFailures": s.metrics["delivery_failures_total"],
                   "exporterErrors": s.metrics["exporter_errors_total"],
                   "trafficErrors": sum(1 for r in rows if r["event_type"] == "TrafficError")},
        "integrity": {"duplicates": s.metrics["events_duplicate_total"],
                      "rejected": rejected_total,
                      "dataGaps": s.metrics["data_gaps_total"],
                      "dataGapsHealed": s.metrics["data_gaps_healed_total"]},
        # легаси-ключи совместимости со старыми потребителями/смоуком:
        "deliveryFailures": s.metrics["delivery_failures_total"],
        "duplicatesCount": s.metrics["events_duplicate_total"],
        "rejectedCount": rejected_total,
        "gapsCount": s.metrics["data_gaps_total"],
    }

    unavailable = [
        {"metric": "forecast",
         "reason": "Модель прогнозирования трафика не развёрнута; прогнозы не выдумываются."},
        {"metric": "sessionDurationSeconds",
         "reason": "До накопления SessionEnded длительность — оценка по первому и последнему "
                   "событию сессии, а не по явному завершению.",
         "estimate": True},
        {"metric": "days[].errors.deliveryFailures / days[].integrity.duplicates|rejected",
         "reason": "Счётчики процесс-глобальные и не атрибутируются по дням; смотрите totals.errors/totals.integrity.",
         "estimate": False},
    ]

    return {
        **totals,
        "window": {"from": day_from.isoformat(), "to": today.isoformat()},
        "days": days,
        "totals": dict(totals),
        "breakdowns": {"byCampaign": tc, "bySource": ts_, "byPage": tp},
        "unavailableMetrics": unavailable,
    }


def _funnel_steps(counts):
    steps = []
    prev = None
    first = None
    for stage in FUNNEL_STAGES:
        cnt = counts.get(stage, 0)
        unavailable = stage in FUNNEL_UNAVAILABLE_STAGES
        if first is None and cnt > 0:
            first = cnt
        conv = None if (prev in (None, 0)) else round(cnt / prev, 3)
        step = {
            "stage": stage,
            "step": stage.lower(),
            "count": cnt,
            "conversionFromPrev": conv,
            "conversionFromFirst": round(cnt / first, 3) if first else None,
            "dropOffRate": None if conv is None else round(max(0.0, 1.0 - conv), 3),
        }
        if unavailable:
            step["stageUnavailable"] = True
        else:
            prev = cnt
        steps.append(step)
    return steps


def compute_funnel(store=None, period_days=7):
    """Честная воронка 8.2: только реальные события, null при нулевом знаменателе,
    stageUnavailable у ступеней без эмиттера. Никаких подстановок из конфига (R3)."""
    s = store or STORE
    period_days = max(1, min(int(period_days), 90))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    placeholders = ",".join("?" for _ in FUNNEL_STAGES)

    with s.get_conn() as conn:
        overall = dict(conn.execute(f"""
            SELECT event_type, COUNT(*) AS cnt FROM events
            WHERE timestamp >= ? AND is_synthetic = 0 AND event_type IN ({placeholders})
            GROUP BY event_type
        """, (cutoff, *FUNNEL_STAGES)).fetchall())
        by_campaign_rows = conn.execute(f"""
            SELECT campaign_id AS k, event_type, COUNT(*) AS cnt FROM events
            WHERE timestamp >= ? AND is_synthetic = 0 AND event_type IN ({placeholders})
            GROUP BY campaign_id, event_type
        """, (cutoff, *FUNNEL_STAGES)).fetchall()
        by_source_rows = conn.execute(f"""
            SELECT source_id AS k, event_type, COUNT(*) AS cnt FROM events
            WHERE timestamp >= ? AND is_synthetic = 0 AND event_type IN ({placeholders})
            GROUP BY source_id, event_type
        """, (cutoff, *FUNNEL_STAGES)).fetchall()

    def group(rows_):
        out = {}
        for r in rows_:
            out.setdefault(r["k"] or "-", {})[r["event_type"]] = r["cnt"]
        return out

    steps = _funnel_steps(overall)
    return {
        "funnelId": "trafficgen_overall",
        "funnelName": "TalkChart Acquisition & Game Conversion Funnel",
        "chain": "offchain",
        "window": {"period": f"{period_days}d UTC"},
        "steps": steps,
        "stages": steps,  # легаси-alias
        "byCampaign": {k: {"steps": _funnel_steps(v)} for k, v in group(by_campaign_rows).items()},
        "bySource": {k: {"steps": _funnel_steps(v)} for k, v in group(by_source_rows).items()},
        "trafficQuality": "hybrid",
        "honestyNote": "counts — только реально принятые события; stageUnavailable=true у ступеней "
                       "без эмиттера; конверсии null при нулевом знаменателе (не выдумываются).",
    }


# ---------------------------------------------------------------------------
# PROMETHEUS
# ---------------------------------------------------------------------------
def build_prometheus_metrics(store=None):
    """Prometheus text format 0.0.4. Счётчики переживают перезапуск (metrics_state + events)."""
    s = store or STORE
    m = s.metrics
    t = m["events_total"]
    rej = m["events_rejected_total"]
    lines = [
        "# HELP trafficgen_events_total Total events ingested by traffic generator",
        "# TYPE trafficgen_events_total counter",
        f'trafficgen_events_total{{source_type="real"}} {t.get("real", 0)}',
        f'trafficgen_events_total{{source_type="bot"}} {t.get("bot", 0)}',
        f'trafficgen_events_total{{source_type="hybrid"}} {t.get("hybrid", 0)}',
        "",
        "# HELP trafficgen_events_duplicate_total Total duplicate events rejected",
        "# TYPE trafficgen_events_duplicate_total counter",
        f"trafficgen_events_duplicate_total {m['events_duplicate_total']}",
        "",
        "# HELP trafficgen_events_rejected_total Total malformed events rejected",
        "# TYPE trafficgen_events_rejected_total counter",
        f'trafficgen_events_rejected_total{{reason="schema"}} {rej.get("schema", 0)}',
        f'trafficgen_events_rejected_total{{reason="pii"}} {rej.get("pii", 0)}',
        f'trafficgen_events_rejected_total{{reason="unknown_type"}} {rej.get("unknown_type", 0)}',
        "",
        "# HELP trafficgen_events_unknown_type_total Total accepted events with unknown eventType (stored partial)",
        "# TYPE trafficgen_events_unknown_type_total counter",
    ]
    unk = m["events_unknown_type"]
    if unk:
        for et, cnt in sorted(unk.items()):
            safe = str(et).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'trafficgen_events_unknown_type_total{{event_type="{safe}"}} {cnt}')
    else:
        lines.append('trafficgen_events_unknown_type_total{event_type="__none__"} 0')
    lines += [
        "",
        "# HELP trafficgen_delivery_failures_total Total failed deliveries",
        "# TYPE trafficgen_delivery_failures_total counter",
        f"trafficgen_delivery_failures_total {m['delivery_failures_total']}",
        "",
        "# HELP trafficgen_exporter_errors_total Total internal exporter errors",
        "# TYPE trafficgen_exporter_errors_total counter",
        f"trafficgen_exporter_errors_total {m['exporter_errors_total']}",
        "",
        "# HELP trafficgen_rate_limited_total Total rate-limited ingestion requests (HTTP 429)",
        "# TYPE trafficgen_rate_limited_total counter",
        f"trafficgen_rate_limited_total {m['rate_limited_total']}",
        "",
        "# HELP trafficgen_buffer_depth Current in-memory buffer depth "
        "(constant 0 by design: no memory queue, synchronous SQLite ingestion)",
        "# TYPE trafficgen_buffer_depth gauge",
        "trafficgen_buffer_depth 0",
        "",
        "# HELP trafficgen_data_gaps_total Total detected sequence gaps",
        "# TYPE trafficgen_data_gaps_total counter",
        f"trafficgen_data_gaps_total {m['data_gaps_total']}",
        "",
        "# HELP trafficgen_data_gaps_healed_total Total gaps closed by backfill",
        "# TYPE trafficgen_data_gaps_healed_total counter",
        f"trafficgen_data_gaps_healed_total {m['data_gaps_healed_total']}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTTP HANDLER (Read-Only Watchtower API + статика + ingestion /api/track)
# ---------------------------------------------------------------------------
SERVER_START_TIME = time.time()


def _watchtower_tokens():
    toks = []
    for var in ("WATCHTOWER_READ_TOKEN", "WATCHTOWER_READ_TOKEN_PREVIOUS"):
        v = os.environ.get(var)
        if v:
            toks.append(v.encode())
    return toks


class _RateLimiter:
    """Token bucket на процесс (глобальный для /api/track)."""

    def __init__(self, rps, burst):
        self.rps = max(0.1, float(rps))
        self.burst = max(1.0, float(burst))
        self.tokens = self.burst
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def drain(self, rps, burst):
        with self.lock:
            self.rps = max(0.1, float(rps))
            self.burst = max(1.0, float(burst))
            self.tokens = self.burst
            self.ts = time.monotonic()

    def allow(self):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rps)
            self.ts = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, 0.0
            return False, max(0.05, (1.0 - self.tokens) / self.rps)


_TRACK_LIMITER = _RateLimiter(RATE_LIMIT_RPS, RATE_LIMIT_BURST)


class WatchtowerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=config.SITE_DIR, **kwargs)

    # --- утилиты --------------------------------------------------------------
    def verify_auth(self):
        tokens = _watchtower_tokens()
        if not tokens:
            return True
        auth_hdr = self.headers.get("Authorization", "")
        provided = auth_hdr[7:].encode() if auth_hdr.startswith("Bearer ") else b""
        return any(provided and hmac.compare_digest(provided, t) for t in tokens)

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", READ_METHODS)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_405(self):
        return self.send_json(405, error_envelope(
            "Method Not Allowed. /watchtower/* is strictly read-only.", "method_not_allowed"),
            extra_headers={"Allow": READ_METHODS})

    def log_message(self, fmt, *args):
        # PII-safe логи: без query-строк и без payload. Полный access-log —
        # только отладочно (WATCHTOWER_DEBUG_LOGS=1) и тоже без тел.
        try:
            msg = str(fmt % args)
        except Exception:
            msg = ""
        path_only = msg.split(" ")[0].split("?")[0] if msg else "-"
        sys.stderr.write("[%s] %s %s\n" % (now_utc_iso(), self.address_string(), path_only))

    # --- CORS preflight --------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        allow = READ_METHODS + (", POST" if self.path.startswith("/api/") else "")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", allow)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # --- пишущие методы: наружу только /api/track ------------------------------
    def do_PUT(self):
        return self.send_405()

    def do_DELETE(self):
        return self.send_405()

    def do_PATCH(self):
        return self.send_405()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path.startswith("/watchtower"):
            # Строгий read-only (4.2). Бывший alias POST /watchtower/ingest удалён.
            return self.send_405()

        if parsed.path == "/api/track":
            # consent/opt-out (10.4): уважаем DNT и Sec-GPC — событие не принимается
            if self.headers.get("DNT") == "1" or self.headers.get("Sec-GPC") == "1":
                return self.send_json(202, {"status": "opted_out", "processed": 0})

            allowed, retry_after = _TRACK_LIMITER.allow()
            if not allowed:
                STORE.bump_metric("rate_limited_total")
                STORE.record_event({  # реальный системный сигнал RateLimited (6.5)
                    "eventType": "RateLimited",
                    "campaignId": "system",
                    "sourceId": "factory_pipeline",
                    "sourceType": "bot",
                    "pageId": "target_terminal",
                    "sessionId": "sess_system_ratelimit",
                    "seq": int(time.time() * 1000),
                    "payload": {"endpoint": "/api/track",
                                "retryAfterSeconds": round(retry_after, 2)},
                })
                return self.send_json(429, error_envelope(
                    "Rate limit exceeded for /api/track", "rate_limited", period="live"),
                    extra_headers={"Retry-After": str(max(1, int(retry_after + 0.5)))})

            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            if length > MAX_TRACK_BYTES:
                return self.send_json(413, {"status": "error",
                                            "error": f"payload too large (> {MAX_TRACK_BYTES} bytes)"})
            try:
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
            except Exception as e:
                STORE.bump_metric("exporter_errors_total")
                return self.send_json(400, {"status": "error", "error": f"Invalid telemetry payload: {e}"})

            events = payload if isinstance(payload, list) else [payload]
            results = [STORE.record_event(ev) for ev in events]
            accepted = sum(1 for r in results if r.get("status") == "accepted")
            duplicates = sum(1 for r in results if r.get("status") == "duplicate")
            rejected = [r for r in results if r.get("status") == "rejected"]
            status_code = 200 if not (rejected and not (accepted or duplicates)) else 422
            return self.send_json(status_code, {
                "status": "ok" if status_code == 200 else "rejected",
                "processed": len(results),
                "accepted": accepted,
                "duplicates": duplicates,
                "rejected": len(rejected),
                "results": results,
            })

        return self.send_json(404, {"error": "Not Found"})

    # --- GET -------------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/watchtower/metrics", "/metrics"):
            if path.startswith("/watchtower") and not self.verify_auth():
                return self.send_json(401, error_envelope(
                    "Unauthorized. Provide valid read token.", "unauthorized", period="live"))
            metrics_txt = build_prometheus_metrics(STORE)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(metrics_txt.encode("utf-8"))))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(metrics_txt.encode("utf-8"))
            return

        if not path.startswith("/watchtower"):
            return super().do_GET()

        if not self.verify_auth():
            return self.send_json(401, error_envelope(
                "Unauthorized. Provide valid read token.", "unauthorized", period="live"))

        if path == "/watchtower/health":
            return self.send_json(200, wrap_envelope({
                "ok": True,  # совместимость с клиентским чек-листом Watchtower (data.ok)
                "status": "ok", "app": APP_ID, "version": PARSER_VERSION,
                "uptimeSeconds": round(time.time() - SERVER_START_TIME, 1),
                "timestamp": now_utc_iso(),
            }, period="live"))

        if path == "/watchtower/readyz":
            db_ok = os.path.exists(STORE.db_path)
            snap_ok = os.path.exists(config.SNAPSHOT)
            ready = db_ok and snap_ok
            return self.send_json(200 if ready else 503, wrap_envelope({
                "ready": ready,
                "checks": {
                    "database": "ok" if db_ok else "unavailable",
                    "snapshot": "ok" if snap_ok else "unavailable",
                    "exporter": "ok",
                },
            }, quality="complete" if ready else "partial",
               confidence=1.0 if ready else 0.5, period="live"))

        if path == "/watchtower/config":
            return self.send_json(200, wrap_envelope(STORE.get_config(), period="snapshot"))

        if path == "/watchtower/campaigns":
            return self.send_json(200, wrap_envelope(
                {"total": len(CAMPAIGNS_DEF), "campaigns": CAMPAIGNS_DEF}, period="snapshot"))

        m_camp = re.match(r"^/watchtower/campaigns/([^/]+)$", path)
        if m_camp:
            cid = m_camp.group(1)
            camp = next((c for c in CAMPAIGNS_DEF if c["id"] == cid), None)
            if camp:
                return self.send_json(200, wrap_envelope(camp, period="snapshot"))
            return self.send_json(404, error_envelope(f"Campaign '{cid}' not found", "not_found"))

        if path == "/watchtower/sources":
            return self.send_json(200, wrap_envelope(
                {"total": len(SOURCES_DEF), "sources": SOURCES_DEF}, period="snapshot"))

        if path == "/watchtower/pages":
            pages = get_target_pages_def()
            return self.send_json(200, wrap_envelope(
                {"total": len(pages), "pages": pages}, period="snapshot"))

        if path == "/watchtower/events":
            cursor = qs.get("cursor", [None])[0]
            try:
                limit = int(qs.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            filters = {
                "eventType": qs.get("eventType", [None])[0],
                "campaignId": qs.get("campaignId", [None])[0],
                "sourceType": qs.get("sourceType", [None])[0],
                "since": qs.get("since", [None])[0],
            }
            try:
                events, next_cursor = STORE.get_events(cursor, limit, filters)
            except InvalidCursor as e:
                return self.send_json(400, error_envelope(
                    f"Invalid cursor: {e}", "invalid_cursor", period="stream"))
            return self.send_json(200, wrap_envelope({
                "events": events, "count": len(events),
                "nextCursor": next_cursor, "hasMore": bool(next_cursor),
            }, period="stream"))

        if path == "/watchtower/metrics/daily":
            period = qs.get("period", ["7d"])[0]
            m_days = re.match(r"^(\d+)d?$", str(period))
            days = int(m_days.group(1)) if m_days else 7
            days = max(1, min(days, 90))
            return self.send_json(200, wrap_envelope(
                compute_metrics(days, STORE), period=f"{days}d UTC"))

        if path == "/watchtower/funnels":
            period = qs.get("period", ["7d"])[0]
            m_days = re.match(r"^(\d+)d?$", str(period))
            days = int(m_days.group(1)) if m_days else 7
            days = max(1, min(days, 90))
            return self.send_json(200, wrap_envelope(
                compute_funnel(STORE, period_days=days), period=f"{days}d UTC"))

        if path == "/watchtower/alerts":
            alerts = STORE.get_alerts()
            active = [a for a in alerts if a.get("status") == "active"]
            return self.send_json(200, wrap_envelope({
                "alerts": alerts,
                "activeCount": len(active),
                "totalCount": len(alerts),
            }, period="7d UTC"))

        if path == "/watchtower/forecast":
            return self.send_json(200, wrap_envelope(
                {"forecast": None, "model": None,
                 "reason": "Модель прогнозирования трафика не развёрнута; прогнозы не выдумываются."},
                quality="unavailable", confidence=0.0))

        return self.send_json(404, error_envelope("Unknown Watchtower endpoint", "not_found"))


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------
def run_server(port=8000, host="0.0.0.0"):
    pruned = STORE.prune_retention()
    if any(pruned.values()):
        print(f"[retention] events: -{pruned['events']}, gaps: -{pruned['gaps']}, "
              f"sessions: -{pruned['sessions']}, aggregates: -{pruned['aggregates']}")
    emitted = reconcile_catalog(STORE)
    print(f"[reconcile] lifecycle-событий записано на этом запуске: {len(emitted)} "
          f"(идемпотентно: повторные запуски ничего не добавляют)")
    print(f"Запуск Watchtower Exporter & Web Server на {host}:{port}...")
    server = ThreadingHTTPServer((host, port), WatchtowerHandler)
    server.serve_forever()


if __name__ == "__main__":
    p = 8000
    if len(sys.argv) > 1:
        try:
            p = int(sys.argv[1])
        except ValueError:
            pass
    run_server(port=p)
