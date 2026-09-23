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
from watchtower_control import (  # контроль-плейн: proposal → 2 подтверждения → apply
    CONTROL, SEVERITY_DICTIONARY, EVENT_SEVERITY, authenticate, control_enabled,
    totp_code, PROPOSAL_TTL_SECONDS,
)
import watchtower_detectors as detectors

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
    ("CampaignStarted", True, None),        # реконсилёр (status=active) + resume после паузы
    ("CampaignStopped", True, None),        # реконсилёр (смена статуса / удаление из конфига)
    ("CampaignUpdated", True, None),        # реконсилёр, proposal campaign_upsert, rollback паузы
    ("SourceConnected", True, None),        # реконсилёр источников
    ("SourceDisconnected", True, None),     # реконсилёр источников
    ("SourceHealthChanged", True, None),    # реконсилёр (смена status)
    ("PageAssigned", True, None),           # реконсилёр страниц (включая games.js)
    ("PageRemoved", True, None),            # реконсилёр страниц
    ("SessionStarted", True, None),         # site/app.js при старте сессии
    ("PageView", True, None),               # site/app.js
    ("Click", True, None),                  # site/app.js (общие клики)
    ("CTAClicked", True, None),             # site/app.js (click_slot, promo)
    ("LandingReached", False,
     "Нет подтверждённого перехода: нужен redirect-proxy/beacon со стороны игры. "
     "Воронку не дорисовываем — нулевые знаменатели остаются null."),
    ("SessionEnded", True, None),           # site/app.js pagehide + sendBeacon
    ("SessionAbandoned", True, None),       # планировщик таймаутов (watchtower_detectors.SessionSweeper)
    ("NavigationCompleted", True, None),    # site/app.js: явный маппинг внутренней навигации терминала
    ("DeliveryFailed", False,
     "Приём синхронный (POST /api/track), очереди доставки нет — событие нечем эмитить."),
    ("RetryScheduled", False,
     "Ретраев нет (синхронный приём); ретраи появятся только вместе с очередью доставки."),
    ("RateLimited", True, None),            # экспортёр при 429 на /api/track
    ("TrafficError", True, None),           # инструментированный конвейер фабрики (emit_traffic_error)
    ("ExporterHealth", True, None),         # периодический self-check (HealthProbe), отдельно от health/readyz
    ("DataGapDetected", True, None),        # gap detector экспортёра
    ("DataGapHealed", True, None),          # закрытие разрыва реальным backfill'ом
    ("BotFlagged", True, None),             # классификатор BotClassifier (правила + порог + журнал решений)
    ("AnomalyDetected", True, None),        # робастный z-score по часовым корзинам (AnomalyDetector)
    ("AbuseBlocked", True, None),           # blocking-слой, включается ТОЛЬКО proposal с двумя подтверждениями
    ("ConfigUpdated", False,
     "Решение зафиксировано явно: изменения конфигурации покрываются гранулярными "
     "событиями реконсилёра (Campaign*/Source*/Page*) и proposal-журналом, отдельного "
     "эмиттера нет и не планируется."),
    ("EmergencyPause", True, None),         # proposal emergency_pause (2 подтверждения + 2FA), не автоматика
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
# Легаси-алиасы eventType: старые записи в БД и старые клиенты не переименовываются,
# но в каталоге и в аналитике тип один — канонический.
EVENT_TYPE_ALIASES = {"Abandoned": "SessionAbandoned"}
VALID_SOURCE_TYPES = {"real", "bot", "hybrid"}
REJECTED_REASON_KEYS = ("schema", "pii", "invalid_timestamp", "non_utc_timestamp",
                        "unknown_type", "paused", "blocked")
# Минимальный объём явных завершений сессий, при котором длительность перестаёт
# быть оценкой (W2: убрать estimate:true, но только по факту, а не по желанию).
MIN_SESSIONS_FOR_DURATION = int(os.environ.get("TRAFFICGEN_MIN_SESSIONS_FOR_DURATION", "30"))

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
    et = EVENT_TYPE_ALIASES.get(et, et)
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
            # Счётчики по дням: заменяют процесс-глобальные (см. unavailableMetrics раньше).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS counters_daily (
                    date TEXT NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (date, key)
                );
            """)
            # DLQ: отклонённые события с причиной — их можно разобрать и, если причина
            # устранена, переобработать. Хранится только PII-безопасный образец.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rejected_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT, event_type TEXT, reason TEXT, message TEXT,
                    campaign_id TEXT, session_id TEXT, source_id TEXT,
                    sample TEXT, created_at TEXT, reprocessed INTEGER DEFAULT 0
                );
            """)
            # Выборка задержек ответа для SLO (p95/p99 считаются по факту, а не «на глаз»).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS latency_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route TEXT NOT NULL, ms REAL NOT NULL, status INTEGER, ts TEXT NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_latency_route ON latency_samples(route, ts);")

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
            key = e.reason if e.reason in REJECTED_REASON_KEYS else "schema"
            self.bump_metric("events_rejected_total", key)
            self.bump_daily_counter(f"rejected.{key}")
            self.record_rejected(raw, key, str(e))
            return {"status": "rejected", "reason": e.reason, "message": str(e),
                    "eventId": raw.get("eventId") if isinstance(raw, dict) else None,
                    "identity": None, "id": None}

        ev = canonicalize_event(raw_pii)
        unknown_type = ev["eventType"] not in CATALOG_EVENT_TYPES
        if unknown_type:
            self.bump_metric("events_rejected_total", "unknown_type")
            self.bump_unknown_type(ev["eventType"])
            self.bump_daily_counter("rejected.unknown_type")

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
            self.bump_daily_counter("duplicate")
            return {"status": "duplicate", "eventId": ev["eventId"],
                    "identity": ev["identity"], "id": dup_row_id}

        st = ev["sourceType"] if ev["sourceType"] in ("real", "bot", "hybrid") else "real"
        self.bump_metric("events_total", st)
        self.bump_daily_counter(f"accepted.{st}")
        if gap_opened:
            self.bump_metric("data_gaps_total")
            self.bump_daily_counter("gap.detected")
        if healed:
            self.bump_metric("data_gaps_healed_total", amount=healed)
            self.bump_daily_counter("gap.healed", amount=healed)
        # Позднее связывание идентичности (d-06): session → externalId/playerKey.
        self.bind_from_event(ev)

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

    # --- счётчики по дням (per-day, а не процесс-глобальные) -------------------
    def bump_daily_counter(self, key, amount=1, date=None):
        """Инкремент суточного счётчика. Вызывать вне write-транзакций."""
        day = date or now_utc_iso()[:10]
        try:
            with self.get_conn() as conn:
                conn.execute("""
                    INSERT INTO counters_daily (date, key, value) VALUES (?, ?, ?)
                    ON CONFLICT(date, key) DO UPDATE SET value = value + excluded.value
                """, (day, key, int(amount)))
                conn.commit()
        except Exception:
            pass

    def counters_for_day(self, day):
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT key, value FROM counters_daily WHERE date = ?", (day,)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def counters_range(self, days):
        out = {}
        for i in range(days):
            d = (datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat()
            out[d] = self.counters_for_day(d)
        return out

    # --- DLQ: отклонённые события ----------------------------------------------
    def record_rejected(self, raw, reason, message):
        """Пишет отклонённое событие в dead-letter с PII-безопасным образцом."""
        try:
            raw = raw if isinstance(raw, dict) else {}
            payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            sample = json.dumps(
                {k: payload[k] for k in list(payload)[:5]}, ensure_ascii=False)[:500]
            with self.get_conn() as conn:
                conn.execute("""
                    INSERT INTO rejected_events
                    (event_id, event_type, reason, message, campaign_id, session_id,
                     source_id, sample, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (raw.get("eventId"), raw.get("eventType"), reason, str(message)[:300],
                      raw.get("campaignId"), raw.get("sessionId"), raw.get("sourceId"),
                      sample, now_utc_iso()))
                conn.commit()
        except Exception:
            pass

    def rejected_stats(self, limit=50):
        with self.get_conn() as conn:
            by_reason = {r["reason"]: r["c"] for r in conn.execute(
                "SELECT reason, COUNT(*) AS c FROM rejected_events GROUP BY reason").fetchall()}
            total = conn.execute("SELECT COUNT(*) AS c FROM rejected_events").fetchone()["c"]
            recent = [dict(r) for r in conn.execute(
                "SELECT id, event_id, event_type, reason, message, created_at "
                "FROM rejected_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        return {"total": total, "byReason": by_reason, "recent": recent}

    # --- задержки ответа (SLO) --------------------------------------------------
    def record_latency(self, route, ms, status=200, cap=5000):
        try:
            with self.get_conn() as conn:
                conn.execute(
                    "INSERT INTO latency_samples (route, ms, status, ts) VALUES (?, ?, ?, ?)",
                    (route, round(float(ms), 2), int(status), now_utc_iso()))
                conn.execute("""
                    DELETE FROM latency_samples WHERE id NOT IN (
                        SELECT id FROM latency_samples ORDER BY id DESC LIMIT ?
                    )
                """, (cap,))
                conn.commit()
        except Exception:
            pass

    def latency_percentiles(self, route=None, since=None):
        query = ["SELECT route, ms FROM latency_samples WHERE 1 = 1"]
        params = []
        if route:
            query.append("AND route = ?")
            params.append(route)
        if since:
            query.append("AND ts >= ?")
            params.append(since)
        with self.get_conn() as conn:
            rows = conn.execute(" ".join(query), params).fetchall()
        values = sorted(r["ms"] for r in rows)
        if not values:
            return {"samples": 0, "p50": None, "p95": None, "p99": None, "max": None}
        return {
            "samples": len(values),
            "p50": round(values[int(0.50 * (len(values) - 1))], 1),
            "p95": round(values[int(0.95 * (len(values) - 1))], 1),
            "p99": round(values[int(0.99 * (len(values) - 1))], 1),
            "max": round(values[-1], 1),
        }

    # --- позднее связывание идентичности (d-06) ---------------------------------
    def bind_from_event(self, ev):
        """session → externalId/playerKey (хеш) → first_action.

        Сырые значения не сохраняются: только sha256 с солью из ENV.
        """
        payload = ev.get("payload") or {}
        ext = payload.get("externalId")
        player = payload.get("playerKey")
        if not ext and not player:
            return None
        salt = os.environ.get("TRAFFICGEN_IDENTITY_SALT", "trafficgen-default-salt")
        def h(v):
            return hashlib.sha256((salt + str(v)).encode("utf-8")).hexdigest()
        try:
            CONTROL.bind_identity(
                ev.get("sessionId"),
                external_id_hash=h(ext or player),
                first_action_at=(ev.get("observedAt") if ev.get("eventType")
                                 in ("Click", "CTAClicked", "PageView") else None),
                campaign_id=ev.get("campaignId"),
                page_id=ev.get("pageId"),
            )
            return True
        except Exception:
            return None

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


TERMINAL_EVENT_TYPES = ("SessionEnded", "SessionAbandoned")


def _session_stats(rows):
    """Статистика по выборке событий. Возвращает (stats, by_campaign, by_source, by_page).

    Длительность сессии считается ТОЧНО только по явному завершению
    (`SessionEnded`/`SessionAbandoned`, при наличии — по `payload.durationSeconds`).
    Пока явных завершений меньше `MIN_SESSIONS_FOR_DURATION`, длительность
    остаётся оценкой по первому и последнему событию и помечена `estimate: true`.
    Сессии экспортёра/детекторов (`sess_system*`) в пользовательскую статистику
    не входят — иначе «бот-трафик» и bounce-rate раздуваются системными событиями.
    """
    sessions = {}
    campaigns = {}
    sources = {}
    pages = {}
    types = {"real": 0, "bot": 0, "hybrid": 0}
    page_views = cta_clicks = landing_reached = 0
    system_events = 0

    for r in rows:
        sid = r["session_id"]
        et = r["event_type"]
        st = r["source_type"] if r["source_type"] in types else "real"
        cid = r["campaign_id"] or "-"
        src = r["source_id"] or "-"
        pid = r["page_id"] or "-"

        if isinstance(sid, str) and sid.startswith("sess_system"):
            system_events += 1
            continue

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

        sessions.setdefault(sid, {"count": 0, "start": r["timestamp"], "end": r["timestamp"],
                                  "types": set(), "terminalAt": None, "reportedDuration": None})
        s = sessions[sid]
        s["count"] += 1
        s["types"].add(st)
        if r["timestamp"] < s["start"]:
            s["start"] = r["timestamp"]
        if r["timestamp"] > s["end"]:
            s["end"] = r["timestamp"]
        if et in TERMINAL_EVENT_TYPES:
            s["terminalAt"] = r["timestamp"]
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                payload = {}
            if isinstance(payload.get("durationSeconds"), (int, float)):
                s["reportedDuration"] = float(payload["durationSeconds"])
        campaigns.setdefault(cid, {"pageViews": 0, "sessions": set(), "ctaClicks": 0, "landingReached": 0})["sessions"].add(sid)
        sources.setdefault(src, {"pageViews": 0, "sessions": set(), "sourceType": st})["sessions"].add(sid)

    exact_durations, span_durations, bounces = [], [], 0
    visitors_by_type = {"real": 0, "bot": 0, "hybrid": 0}
    for s in sessions.values():
        if s["count"] == 1:
            bounces += 1
        st = "bot" if "bot" in s["types"] else ("hybrid" if "hybrid" in s["types"] else "real")
        visitors_by_type[st] += 1
        if s["reportedDuration"] is not None:
            exact_durations.append(max(0.0, s["reportedDuration"]))
            continue
        t0 = _lenient_ts(s["start"])
        if s["terminalAt"]:
            t1 = _lenient_ts(s["terminalAt"])
            if t0 and t1:
                exact_durations.append(max(0.0, (t1 - t0).total_seconds()))
                continue
        t1 = _lenient_ts(s["end"])
        if t0 and t1:
            span_durations.append(max(0.0, (t1 - t0).total_seconds()))
    exact_durations.sort()
    span_durations.sort()

    enough_exact = len(exact_durations) >= MIN_SESSIONS_FOR_DURATION
    durations = exact_durations if enough_exact else (exact_durations + span_durations)
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
            "estimate": not enough_exact,
            "sampleSize": len(durations),
            "explicitCompletions": len(exact_durations),
            "minSessionsForExact": MIN_SESSIONS_FOR_DURATION,
        },
        "bounceRate": round(bounces / n_sessions, 3) if n_sessions else 0.0,
        "ctaClickRate": round(cta_clicks / page_views, 3) if page_views else 0.0,
        "landingReachedRate": round(landing_reached / cta_clicks, 3) if cta_clicks else 0.0,
        "trafficType": types,
        "systemEventsExcluded": system_events,
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

    counters = s.counters_range(period_days)

    days = []
    for i in range(period_days):
        d_iso = (day_from + timedelta(days=i)).isoformat()
        d_rows = by_day.get(d_iso, [])
        day_counters = counters.get(d_iso, {})
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
                # per-day счётчики (counters_daily), а не процесс-глобальные
                "rejectedSchema": day_counters.get("rejected.schema", 0),
                "rejectedPii": day_counters.get("rejected.pii", 0),
                "rejectedUnknownType": day_counters.get("rejected.unknown_type", 0),
                "rejectedTimestamp": day_counters.get("rejected.invalid_timestamp", 0)
                + day_counters.get("rejected.non_utc_timestamp", 0),
                "rateLimited": day_counters.get("rate_limited", 0),
                "paused": day_counters.get("paused", 0),
                "blocked": day_counters.get("blocked", 0),
                "trafficErrors": sum(1 for r in d_rows if r["event_type"] == "TrafficError"),
            },
            "integrity": {
                "duplicates": day_counters.get("duplicate", 0),
                "rejected": sum(v for k, v in day_counters.items() if k.startswith("rejected.")),
                "accepted": day_counters.get("accepted.real", 0) + day_counters.get("accepted.bot", 0)
                + day_counters.get("accepted.hybrid", 0),
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

    # Честный список недоступного: то, что закрыто (per-day счётчики), из списка
    # удалено — оставлять запись о проблеме, которой нет, значит врать в обе стороны.
    unavailable = [
        {"metric": "forecast",
         "reason": "Модель прогнозирования трафика не развёрнута; прогнозы не выдумываются."},
    ]
    if stats_total["sessionDurationSeconds"]["estimate"]:
        unavailable.append({
            "metric": "sessionDurationSeconds",
            "reason": f"Явных завершений сессий {stats_total['sessionDurationSeconds']['explicitCompletions']} "
                      f"— меньше порога {MIN_SESSIONS_FOR_DURATION}. Длительность — оценка по "
                      "первому и последнему событию сессии, а не по SessionEnded/SessionAbandoned.",
            "estimate": True,
        })
    unavailable.append({
        "metric": "deliveryFailures / retryScheduled",
        "reason": "Очереди доставки нет (приём синхронный) — события DeliveryFailed и "
                  "RetryScheduled нечем эмитить; они остаются unavailable в каталоге.",
        "estimate": False,
    })
    unavailable.append({
        "metric": "landingReachedRate",
        "reason": "LandingReached unavailable: нет подтверждённого перехода из игры. "
                  "Знаменатель остаётся null, воронка не дорисовывается.",
        "estimate": False,
    })

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



def compute_acquisition_funnel(store=None, period_days=7):
    """Сквозная acquisition-воронка (контур i-09).

    ad_click → visit → page_view → identity_bound → first_action → retained.

    Честность прежде удобства: «ad_click» считается по PageView с атрибуцией
    кампании (реального клика по объявлению мы не видим — смотри unavailable у
    шага). Конверсия считается только от ненулевого знаменателя: иначе это не
    конверсия, а число, нарисованное на пустом месте.
    """
    s = store or STORE
    period_days = max(1, min(int(period_days), 90))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    with s.get_conn() as conn:
        visits = conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS c FROM events "
            "WHERE timestamp >= ? AND is_synthetic = 0 AND event_type = 'SessionStarted' "
            "AND session_id NOT LIKE 'sess_system%'", (cutoff,)).fetchone()["c"]
        views = conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS c FROM events "
            "WHERE timestamp >= ? AND is_synthetic = 0 AND event_type = 'PageView' "
            "AND session_id NOT LIKE 'sess_system%'", (cutoff,)).fetchone()["c"]
        actions = conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS c FROM events "
            "WHERE timestamp >= ? AND is_synthetic = 0 AND event_type IN ('Click', 'CTAClicked') "
            "AND session_id NOT LIKE 'sess_system%'", (cutoff,)).fetchone()["c"]
        retained = conn.execute(
            "SELECT COUNT(*) AS c FROM (SELECT session_id FROM events "
            "WHERE timestamp >= ? AND is_synthetic = 0 "
            "AND session_id NOT LIKE 'sess_system%' "
            "GROUP BY session_id HAVING COUNT(DISTINCT substr(timestamp, 1, 10)) >= 2)",
            (cutoff,)).fetchone()["c"]
    bound = CONTROL.identity_stats()
    bound_sessions = bound.get("boundToExternalId", 0)

    raw_steps = [
        ("ad_click", views, "PageView с атрибуцией кампании; реального клика по объявлению "
                            "нет — шаг является верхней границей, а не фактом клика", False),
        ("visit", visits, None, True),
        ("page_view", views, None, True),
        ("identity_bound", bound_sessions, None, True),
        ("first_action", actions, None, True),
        ("retained", retained, "сессии с активностью в ≥2 разных днях", True),
    ]
    steps = []
    prev = None
    for name, cnt, note, available in raw_steps:
        conv = None if prev in (None, 0) else round(cnt / prev, 3)
        step = {"stage": name, "count": cnt, "conversionFromPrev": conv,
                "available": available}
        if note:
            step["note"] = note
        steps.append(step)
        if available:
            prev = cnt
    return {
        "funnelId": "trafficgen_acquisition",
        "window": {"period": f"{period_days}d UTC"},
        "steps": steps,
        "honestyNote": "identity_bound считается по сессиям, у которых есть externalId/playerKey; "
                       "сырые идентификаторы не хранятся (sha256+salt).",
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
# ЭМИССИЯ СОБЫТИЙ ИЗ КОНТРОЛЬ-ПЛЕЙНА
# ---------------------------------------------------------------------------
def emit_control_event(kind, payload, effect, proposal_id, actor):
    """Превращает применённый proposal в событие каталога.

    Никакой автоматики: сюда попадает только то, что подтвердили два человека
    с 2FA. Откат тоже эмитит событие — иначе в истории останется «дырка».
    """
    ts = now_utc_iso()
    effect = effect or {}
    common = {"proposalId": proposal_id, "actor": actor, "effect": effect}

    # Откат — отдельная запись в истории, а не «исчезновение» предыдущей.
    if effect.get("rolledBack") is True:
        if kind == "emergency_pause":
            camp = payload.get("campaignId")
            return detectors.emit_system_event(
                STORE, "CampaignUpdated",
                {**common, "campaignId": camp, "changedFields": ["status"],
                 "status": "active", "resumed": True, "rolledBack": True},
                session="sess_system_control", campaign_id=camp or "system",
                identity=f"offchain:trafficgen:{camp or 'system'}:target_terminal:"
                         f"sess_system_control:rollback:{proposal_id}")
        if kind == "campaign_upsert":
            camp = payload.get("campaign") or {}
            cid = camp.get("id") or "unknown"
            return detectors.emit_system_event(
                STORE, "CampaignUpdated",
                {**common, "campaignId": cid, "rolledBack": True,
                 "changedFields": ["definition"]},
                session="sess_system_control", campaign_id=cid,
                identity=f"offchain:trafficgen:{cid}:target_terminal:"
                         f"sess_system_control:rollback:{proposal_id}")
        # Снятие блокировки (unblock) не имеет события в каноническом каталоге:
        # AbuseBlocked означает «заблокировано», а не «изменили настройку».
        # Факт остаётся в audit-журнале и в /watchtower/proposals.
        return None

    if kind == "emergency_pause":
        camp = payload.get("campaignId")
        return detectors.emit_system_event(
            STORE, "EmergencyPause",
            {**common, "campaignId": camp, "reason": payload.get("reason"),
             "durationMinutes": payload.get("durationMinutes", 120),
             "until": (effect or {}).get("until")},
            session="sess_system_control", campaign_id=camp or "system",
            identity=f"offchain:trafficgen:{camp or 'system'}:target_terminal:"
                     f"sess_system_control:pause:{proposal_id}")
    if kind == "resume_campaign":
        camp = payload.get("campaignId")
        return detectors.emit_system_event(
            STORE, "CampaignUpdated",
            {**common, "campaignId": camp, "changedFields": ["status"],
             "status": "active", "resumed": True},
            session="sess_system_control", campaign_id=camp or "system",
            identity=f"offchain:trafficgen:{camp or 'system'}:target_terminal:"
                     f"sess_system_control:resume:{proposal_id}")
    if kind in ("block_source", "block_session"):
        target = payload.get("sourceId") or payload.get("sessionId")
        target_type = "source" if kind == "block_source" else "session"
        return detectors.emit_system_event(
            STORE, "AbuseBlocked",
            {**common, "targetType": target_type, "targetRef": target,
             "reason": payload.get("reason"),
             "ttlMinutes": payload.get("ttlMinutes", 1440),
             "expiresAt": (effect or {}).get("expiresAt")},
            session="sess_system_control",
            identity=f"offchain:trafficgen:system:target_terminal:"
                     f"sess_system_control:block:{proposal_id}")
    if kind == "campaign_upsert":
        camp = payload.get("campaign") or {}
        cid = camp.get("id") or "unknown"
        created = (effect or {}).get("created") is True
        return detectors.emit_system_event(
            STORE, "CampaignCreated" if created else "CampaignUpdated",
            {**common, "campaignId": cid, "name": camp.get("name"),
             "segments": camp.get("segments"),
             "frequencyCap": camp.get("frequencyCap"),
             "budget": camp.get("budget"),
             "attribution": camp.get("attribution"),
             "source": "hub_proposal"},
            session="sess_system_control", campaign_id=cid,
            identity=f"offchain:trafficgen:{cid}:target_terminal:"
                     f"sess_system_control:upsert:{proposal_id}")
    return None


# ---------------------------------------------------------------------------
# HTTP HANDLER (Read-Only Watchtower API + статика + ingestion /api/track)
# ---------------------------------------------------------------------------
SERVER_START_TIME = time.time()
# Планировщик детекторов: назначается в run_server(), читается маршрутом
# /watchtower/detectors. None — значит детекторы выключены флагом.
SCHEDULER = None


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
        # Замер задержки ответа — основа SLO (p95/p99 считаются по факту).
        label = getattr(self, "_route_label", None)
        if label:
            try:
                STORE.record_latency(label, (time.monotonic() - self._t0) * 1000.0, status)
            except Exception:
                pass
            self._route_label = None
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
            self._t0 = time.monotonic()
            self._route_label = "track"
            # consent/opt-out (10.4): уважаем DNT и Sec-GPC — событие не принимается
            if self.headers.get("DNT") == "1" or self.headers.get("Sec-GPC") == "1":
                return self.send_json(202, {"status": "opted_out", "processed": 0})

            allowed, retry_after = _TRACK_LIMITER.allow()
            if not allowed:
                STORE.bump_metric("rate_limited_total")
                STORE.bump_daily_counter("rate_limited")
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
            results = []
            accepted = duplicates = paused = blocked = 0
            for ev in events:
                if not isinstance(ev, dict):
                    results.append({"status": "rejected", "reason": "schema",
                                    "message": "event must be an object"})
                    continue
                camp = ev.get("campaignId") or "talkchart_interactive_radar"
                sid = ev.get("sessionId")
                src = ev.get("sourceId") or "direct_web"
                # Пауза кампании и блокировки — только следствие подтверждённого
                # proposal'а: автоматической блокировки здесь не существует.
                if CONTROL.is_paused(camp):
                    paused += 1
                    STORE.bump_daily_counter("paused")
                    results.append({"status": "paused",
                                    "reason": f"campaign '{camp}' is paused by proposal"})
                    continue
                if CONTROL.is_blocked("session", sid) or CONTROL.is_blocked("source", src):
                    blocked += 1
                    STORE.bump_daily_counter("blocked")
                    results.append({"status": "blocked",
                                    "reason": "session or source blocked by proposal"})
                    continue
                res = STORE.record_event(ev)
                results.append(res)
                if res.get("status") == "accepted":
                    accepted += 1
                elif res.get("status") == "duplicate":
                    duplicates += 1
            rejected = [r for r in results if r.get("status") == "rejected"]
            status_code = 200 if not (rejected and not (accepted or duplicates)) else 422
            return self.send_json(status_code, {
                "status": "ok" if status_code == 200 else "rejected",
                "processed": len(results),
                "accepted": accepted,
                "duplicates": duplicates,
                "rejected": len(rejected),
                "paused": paused,
                "blocked": blocked,
                "results": results,
            })

        if parsed.path.startswith("/api/control/"):
            return self.handle_control(parsed)

        return self.send_json(404, {"error": "Not Found"})


    # --- контроль-плейн: proposal → 2 подтверждения → apply → rollback ---------
    def _control_actor(self):
        auth_hdr = self.headers.get("Authorization", "")
        token = auth_hdr[7:] if auth_hdr.startswith("Bearer ") else ""
        return authenticate(token)

    def _control_unavailable(self):
        return self.send_json(503, error_envelope(
            "Контроль-плейн недоступен: не задан TRAFFICGEN_CONTROL_USERS "
            "(JSON: пользователь -> {token, role, secret}). Без этого никто не имеет "
            "права применять изменения, поэтому эндпоинт честно недоступен.",
            "control_unavailable", period="live"))

    def handle_control(self, parsed):
        started = time.monotonic()
        try:
            if not control_enabled():
                return self._control_unavailable()
            actor_info = self._control_actor()
            if actor_info is None:
                return self.send_json(401, error_envelope(
                    "Unauthorized. Нужен Bearer-токен из TRAFFICGEN_CONTROL_USERS.",
                    "unauthorized", period="live"))
            user, role, secret = actor_info

            body = self._read_json_body(64 * 1024)
            if body is None:
                return self.send_json(400, error_envelope(
                    "Тело запроса должно быть JSON-объектом", "bad_request", period="live"))

            path = parsed.path
            if path == "/api/control/proposals":
                proposal, err = CONTROL.create_proposal(
                    body.get("kind"), body.get("payload") or {}, user, role,
                    reason=body.get("reason"), suggested_by=body.get("suggestedBy"))
                if err:
                    return self.send_json(422, error_envelope(
                        err["message"], err["code"], period="live"))
                return self.send_json(201, wrap_envelope(proposal, period="live"))

            m = re.match(r"^/api/control/proposals/([A-Za-z0-9_]+)/(approve|confirm|rollback)$", path)
            if m:
                pid, action = m.group(1), m.group(2)
                code = str(body.get("totp") or body.get("code") or "")
                if action == "approve":
                    proposal, err = CONTROL.approve(pid, user, role, code, secret)
                elif action == "confirm":
                    proposal, err = CONTROL.confirm(pid, user, role, code, secret,
                                                    emit_fn=emit_control_event)
                else:
                    proposal, err = CONTROL.rollback(pid, user, role, code, secret,
                                                     emit_fn=emit_control_event,
                                                     reason=body.get("reason"))
                if err:
                    status = 409 if err["code"] in ("invalid_state", "self_approval_denied",
                                                    "two_person_rule", "duplicate_approval",
                                                    "totp_replay") else (
                        403 if err["code"] == "forbidden" else (
                            401 if err["code"] == "invalid_2fa" else 422))
                    return self.send_json(status, error_envelope(
                        err["message"], err["code"], period="live"))
                return self.send_json(200, wrap_envelope(proposal, period="live"))

            if path == "/api/control/consent":
                subject = body.get("subjectHash") or body.get("subject")
                decision = body.get("decision")
                if not subject or decision not in ("granted", "denied", "opt_out"):
                    return self.send_json(422, error_envelope(
                        "Нужны subjectHash и decision (granted|denied|opt_out)",
                        "bad_request", period="live"))
                state = CONTROL.record_consent(
                    subject, decision, source=body.get("source") or user,
                    version=body.get("version") or "1", details=body.get("details"))
                return self.send_json(200, wrap_envelope(state, period="live"))

            return self.send_json(404, error_envelope("Unknown control endpoint", "not_found"))
        finally:
            STORE.record_latency("control", (time.monotonic() - started) * 1000.0)

    def _read_json_body(self, limit):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            return {} if length <= 0 else None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

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

        self._t0 = time.monotonic()
        self._route_label = path

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
            return self.send_json(200, wrap_envelope({
                **compute_funnel(STORE, period_days=days),
                "acquisition": compute_acquisition_funnel(STORE, period_days=days),
            }, period=f"{days}d UTC"))

        if path == "/watchtower/alerts":
            alerts = STORE.get_alerts()
            active = [a for a in alerts if a.get("status") == "active"]
            return self.send_json(200, wrap_envelope({
                "alerts": alerts,
                "activeCount": len(active),
                "totalCount": len(alerts),
            }, period="7d UTC"))


        if path == "/watchtower/proposals":
            status = qs.get("status", [None])[0]
            return self.send_json(200, wrap_envelope({
                "enabled": control_enabled(),
                "ttlSeconds": PROPOSAL_TTL_SECONDS,
                "requiredApprovals": 2,
                "twoFactor": "totp",
                "proposals": CONTROL.list_proposals(status=status, limit=100),
            }, period="7d UTC"))

        if path == "/watchtower/audit":
            chain = CONTROL.verify_audit_chain()
            return self.send_json(200, wrap_envelope({
                "chain": chain,
                "entries": CONTROL.recent_audit(limit=200),
            }, quality="complete" if chain["valid"] else "partial",
               confidence=1.0 if chain["valid"] else 0.4, period="7d UTC"))

        if path == "/watchtower/blocks":
            return self.send_json(200, wrap_envelope({
                "active": CONTROL.active_blocks(),
                "pausedCampaigns": CONTROL.paused_campaigns(),
                "note": "Блокировки и паузы создаются только подтверждённым proposal; "
                        "автоматического применения нет.",
            }, period="live"))

        if path == "/watchtower/consent":
            return self.send_json(200, wrap_envelope({
                "model": "opt-out: DNT/GPC/параметр notrack останавливают приём до записи в БД",
                "state": CONTROL.consent_state(),
                "syncEndpoint": "/api/control/consent",
            }, period="7d UTC"))

        if path == "/watchtower/identity":
            return self.send_json(200, wrap_envelope({
                "binding": "session → externalId/playerKey (sha256+salt) → first_action",
                "stats": CONTROL.identity_stats(),
            }, period="7d UTC"))

        if path == "/watchtower/forensics":
            return self.send_json(200, wrap_envelope({
                "detectors": CONTROL.detector_quality(),
                "decisionsNote": "Метки-прокси (factory_pipeline → bot) помечаются явно; "
                                 "человеческая разметка проставляется через labelDecision().",
                "rejected": STORE.rejected_stats(limit=50),
            }, period="7d UTC"))

        if path == "/watchtower/severity":
            return self.send_json(200, wrap_envelope({
                "dictionary": SEVERITY_DICTIONARY,
                "eventSeverity": EVENT_SEVERITY,
                "runbook": "docs/SLO_TRAFFICGEN.md#runbook",
            }, period="live"))

        if path == "/watchtower/detectors":
            return self.send_json(200, wrap_envelope(
                SCHEDULER.status() if SCHEDULER is not None else
                {"enabled": False, "reason": "планировщик не запущен (--no-detectors)"},
                period="live"))

        if path == "/watchtower/quality":
            since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return self.send_json(200, wrap_envelope({
                "latency": {
                    "api": STORE.latency_percentiles(since=since),
                    "track": STORE.latency_percentiles(route="track", since=since),
                    "control": STORE.latency_percentiles(route="control", since=since),
                },
                "deadLetter": STORE.rejected_stats(limit=20),
                "countersToday": STORE.counters_for_day(now_utc_iso()[:10]),
            }, period="24h UTC"))

        if path == "/watchtower/forecast":
            return self.send_json(200, wrap_envelope(
                {"forecast": None, "model": None,
                 "reason": "Модель прогнозирования трафика не развёрнута; прогнозы не выдумываются."},
                quality="unavailable", confidence=0.0))

        return self.send_json(404, error_envelope("Unknown Watchtower endpoint", "not_found"))


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------
def prune_once():
    """Разовый проход ретеншена с отчётом. Вызывается из cron/CI, а не при старте.

    Раньше прайнинг висел в run_server(): он съедал время старта, а отчёт о нём
    терялся в логах. Теперь это отдельная команда с машиночитаемым отчётом.
    """
    pruned = STORE.prune_retention()
    report = {
        "ranAt": now_utc_iso(),
        "retentionDays": {"events": EVENT_RETENTION_DAYS, "aggregates": AGGREGATE_RETENTION_DAYS},
        "removed": pruned,
        "rejectedDlq": STORE.rejected_stats(limit=0)["total"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    report_path = os.path.join(config.DATA_DIR, "prune-report.json")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def run_server(port=8000, host="0.0.0.0", start_detectors=True, prune=False):
    global SCHEDULER
    if prune:
        prune_once()
    emitted = reconcile_catalog(STORE)
    print(f"[reconcile] lifecycle-событий записано на этом запуске: {len(emitted)} "
          f"(идемпотентно: повторные запуски ничего не добавляют)")
    if start_detectors:
        SCHEDULER = detectors.DetectorScheduler(STORE, CONTROL, snapshot_path=config.SNAPSHOT)
        started = SCHEDULER.start()
        print(f"[detectors] запущены: {', '.join(started) if started else 'выключены (TRAFFICGEN_DETECTORS=0)'}")
    print(f"Запуск Watchtower Exporter & Web Server на {host}:{port}...")
    server = ThreadingHTTPServer((host, port), WatchtowerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if SCHEDULER:
            SCHEDULER.stop()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port = 8000
    start_detectors = True
    prune = False
    for arg in argv:
        if arg == "--no-detectors":
            start_detectors = False
        elif arg == "--prune":
            prune = True
        elif arg.isdigit():
            port = int(arg)
        elif arg in ("-h", "--help"):
            print(__doc__ or "")
            print("Использование: python3 site/factory/watchtower_exporter.py "
                  "[порт] [--no-detectors] [--prune]")
            print("  --prune          разовый проход ретеншена с отчётом (для cron), без запуска сервера")
            print("  --no-detectors   не запускать фоновые детекторы (например, в тестах)")
            return 0
    if prune and not start_detectors:
        # `--prune` как отдельная команда: сервер не поднимаем.
        prune_once()
        return 0
    run_server(port=port, start_detectors=start_detectors, prune=prune)
    return 0


if __name__ == "__main__":
    sys.exit(main())
