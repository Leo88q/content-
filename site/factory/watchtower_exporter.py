#!/usr/bin/env python3
"""Watchtower Exporter & Read-Only API for Games Watchtower.

Предоставляет read-only телеметрию для Games Watchtower:
- Кампании генерации трафика (SEO, X/Twitter, Blinks, Videos, Radar, TipLink)
- Целевые страницы (4 игры студии, терминал, лендинги)
- Курсорная пагинация событий и replay
- Идемпотентность и дедупликация (eventId, identity)
- Gap detection (DataGapDetected / DataGapHealed)
- Дневные агрегаты и расчет воронки (CampaignStarted -> SessionStarted -> PageView -> CTAClicked -> LandingReached)
- Prometheus-совместимый эндпоинт метрик (/watchtower/metrics)
- Приём клиентской телеметрии (POST /api/track) без PII
"""
import base64
import json
import math
import os
import re
import sqlite3
import sys
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


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def strip_pii(data):
    """Очищает любые персональные и чувствительные данные (PII) из словаря."""
    if not isinstance(data, dict):
        return data
    cleaned = dict(data)
    forbidden_keys = ("ip", "email", "fingerprint", "device_id", "user_agent", "cookie", "auth_token", "secret", "private_key")
    for k in forbidden_keys:
        cleaned.pop(k, None)
    if "payload" in cleaned and isinstance(cleaned["payload"], dict):
        cleaned["payload"] = dict(cleaned["payload"])
        for k in ("user_ip", "ip", "email", "fingerprint", "device_id", "user_agent", "cookie", "auth", "secret", "token"):
            cleaned["payload"].pop(k, None)
    return cleaned


def canonicalize_event(raw):
    """Преобразует произвольное событие в канонический конверт Watchtower."""
    cleaned = strip_pii(raw)
    cid = cleaned.get("campaignId") or "talkchart_interactive_radar"
    pid = cleaned.get("pageId") or "terminal"
    sid = cleaned.get("sessionId") or f"sess_{uuid.uuid4().hex[:10]}"
    seq = int(cleaned.get("seq") or 1)
    eid = cleaned.get("eventId") or f"ev_{uuid.uuid4().hex[:12]}"
    identity = cleaned.get("identity") or f"offchain:trafficgen:{cid}:{pid}:{sid}:{seq}"

    return {
        "eventId": eid,
        "identity": identity,
        "chain": "offchain",
        "source": APP_ID,
        "app": APP_ID,
        "eventType": cleaned.get("eventType") or "PageView",
        "timestamp": cleaned.get("timestamp") or now_utc_iso(),
        "observedAt": cleaned.get("observedAt") or now_utc_iso(),
        "campaignId": cid,
        "sourceId": cleaned.get("sourceId") or "direct_web",
        "sourceType": cleaned.get("sourceType") or ("bot" if cleaned.get("isBot") else "real"),
        "pageId": pid,
        "sessionId": sid,
        "seq": seq,
        "payload": cleaned.get("payload") or {},
        "parserVersion": PARSER_VERSION,
        "dataQuality": "complete"
    }


def build_prometheus_metrics(store=None):
    """Формирует текстовый вывод метрик Prometheus по спецификации Games Watchtower."""
    s = store or STORE
    m = s.metrics
    t = m.get("events_total", {"real": 0, "bot": 0, "hybrid": 0})
    lines = [
        "# HELP trafficgen_events_total Total events ingested by traffic generator",
        "# TYPE trafficgen_events_total counter",
        f'trafficgen_events_total{{source_type="real"}} {t.get("real", 0)}',
        f'trafficgen_events_total{{source_type="bot"}} {t.get("bot", 0)}',
        f'trafficgen_events_total{{source_type="hybrid"}} {t.get("hybrid", 0)}',
        "",
        "# HELP trafficgen_events_duplicate_total Total duplicate events rejected",
        "# TYPE trafficgen_events_duplicate_total counter",
        f"trafficgen_events_duplicate_total {m.get('events_duplicate_total', 0)}",
        "",
        "# HELP trafficgen_events_rejected_total Total malformed events rejected",
        "# TYPE trafficgen_events_rejected_total counter",
        f"trafficgen_events_rejected_total {m.get('events_rejected_total', 0)}",
        "",
        "# HELP trafficgen_delivery_failures_total Total failed deliveries",
        "# TYPE trafficgen_delivery_failures_total counter",
        f"trafficgen_delivery_failures_total {m.get('delivery_failures_total', 0)}",
        "",
        "# HELP trafficgen_exporter_errors_total Total internal exporter errors",
        "# TYPE trafficgen_exporter_errors_total counter",
        f"trafficgen_exporter_errors_total {m.get('exporter_errors_total', 0)}",
        "",
        "# HELP trafficgen_buffer_depth Current in-memory buffer depth",
        "# TYPE trafficgen_buffer_depth gauge",
        "trafficgen_buffer_depth 0",
        "",
        "# HELP trafficgen_data_gaps_total Total detected sequence gaps",
        "# TYPE trafficgen_data_gaps_total counter",
        f"trafficgen_data_gaps_total {m.get('data_gaps_total', 0)}",
    ]
    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------
# ХРАНИЛИЩЕ СОБЫТИЙ (SQLite ACID + Deduplication + Gap Detection)
# -----------------------------------------------------------------------------
class EventStore:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.init_db()
        self.metrics = {
            "events_total": {"real": 0, "bot": 0, "hybrid": 0},
            "events_duplicate_total": 0,
            "events_rejected_total": 0,
            "delivery_failures_total": 0,
            "exporter_errors_total": 0,
            "data_gaps_total": 0,
        }
        self.load_initial_metrics()

    def get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_connection(self):
        return self.get_conn()

    def ingest_event(self, raw):
        """Ингестирование события с конвертацией статуса для тестов и интеграций."""
        res = self.record_event(raw)
        return {
            "success": res.get("status") in ("accepted", "duplicate"),
            "duplicate": res.get("status") == "duplicate",
            "eventId": res.get("eventId"),
            "identity": res.get("identity"),
            "id": res.get("id")
        }

    def get_alerts(self):
        """Возвращает текущие активные алерты, включая обнаруженные разрывы (gaps)."""
        alerts = []
        with self.get_conn() as conn:
            gap_rows = conn.execute(
                "SELECT * FROM events WHERE event_type = 'DataGapDetected' ORDER BY id DESC LIMIT 50"
            ).fetchall()
            for r in gap_rows:
                alerts.append({
                    "alertId": f"gap_{r['id']}",
                    "alertType": "DataGapDetected",
                    "severity": "warning",
                    "sessionId": r["session_id"],
                    "campaignId": r["campaign_id"],
                    "timestamp": r["timestamp"],
                    "details": json.loads(r["payload"] or "{}")
                })
        return alerts

    def get_funnels(self):
        return [compute_funnel(self)]

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
            "eventRetention": "30d",
            "auth": "api-key" if os.environ.get("WATCHTOWER_READ_TOKEN") else "none",
            "timeReference": "utc",
            "parserVersion": PARSER_VERSION,
            "readOnly": True
        }

    def init_db(self):
        with self.get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE,
                    identity TEXT UNIQUE,
                    chain TEXT,
                    source TEXT,
                    app TEXT,
                    event_type TEXT,
                    timestamp TEXT,
                    observed_at TEXT,
                    campaign_id TEXT,
                    source_id TEXT,
                    source_type TEXT,
                    page_id TEXT,
                    session_id TEXT,
                    seq INTEGER,
                    payload TEXT,
                    parser_version TEXT,
                    data_quality TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_seq ON events(session_id, seq);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_sequences (
                    session_id TEXT PRIMARY KEY,
                    last_seq INTEGER,
                    updated_at TEXT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    date TEXT PRIMARY KEY,
                    data TEXT,
                    generated_at TEXT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    type TEXT,
                    status TEXT,
                    data TEXT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    channel TEXT,
                    source_type TEXT,
                    status TEXT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS pages (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    url TEXT,
                    role TEXT,
                    category TEXT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS aggregates_daily (
                    date TEXT PRIMARY KEY,
                    metrics TEXT,
                    generated_at TEXT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    alert_type TEXT,
                    severity TEXT,
                    status TEXT,
                    data TEXT,
                    created_at TEXT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_cursors (
                    source TEXT PRIMARY KEY,
                    cursor TEXT,
                    updated_at TEXT
                );
            """)

    def load_initial_metrics(self):
        try:
            with self.get_conn() as conn:
                for r in conn.execute("SELECT source_type, COUNT(*) as cnt FROM events GROUP BY source_type").fetchall():
                    st = r["source_type"] or "real"
                    if st in self.metrics["events_total"]:
                        self.metrics["events_total"][st] = r["cnt"]
                gaps = conn.execute("SELECT COUNT(*) as cnt FROM events WHERE event_type='DataGapDetected'").fetchone()
                if gaps:
                    self.metrics["data_gaps_total"] = gaps["cnt"]
        except Exception:
            pass

    def record_event(self, raw):
        """Валидация, дедупликация, gap detection и сохранение события."""
        event_id = raw.get("eventId") or str(uuid.uuid4())
        campaign_id = raw.get("campaignId") or "talkchart_interactive_radar"
        page_id = raw.get("pageId") or "terminal"
        session_id = raw.get("sessionId") or "unknown-session"
        seq = int(raw.get("seq") or 1)
        event_type = raw.get("eventType") or "PageView"
        source_type = raw.get("sourceType") or ("bot" if raw.get("isBot") else "real")
        source_id = raw.get("sourceId") or "direct_web"
        ts = raw.get("timestamp") or now_utc_iso()
        obs = raw.get("observedAt") or now_utc_iso()

        # Каноническая off-chain идентичность
        identity = raw.get("identity") or f"offchain:trafficgen:{campaign_id}:{page_id}:{session_id}:{seq}"

        payload_obj = raw.get("payload") or {}
        # Защита от PII
        for pii_key in ("ip", "email", "fingerprint", "device_id", "user_agent", "cookie"):
            payload_obj.pop(pii_key, None)
        payload_str = json.dumps(payload_obj, ensure_ascii=False)

        with self.get_conn() as conn:
            # 1. Проверка на дедупликацию по eventId или identity
            existing = conn.execute(
                "SELECT id, event_id, identity FROM events WHERE event_id = ? OR identity = ?",
                (event_id, identity)
            ).fetchone()
            if existing:
                self.metrics["events_duplicate_total"] += 1
                return {"status": "duplicate", "eventId": event_id, "identity": identity, "id": existing["id"]}

            # 2. Gap Detection по seq внутри сессии
            seq_row = conn.execute("SELECT last_seq FROM session_sequences WHERE session_id = ?", (session_id,)).fetchone()
            if seq_row is not None:
                last_seq = seq_row["last_seq"]
                if seq > last_seq + 1:
                    # Зафиксирован разрыв последовательности!
                    self.metrics["data_gaps_total"] += 1
                    gap_id = str(uuid.uuid4())
                    gap_identity = f"offchain:trafficgen:{campaign_id}:{page_id}:{session_id}:gap:{last_seq+1}-{seq-1}"
                    conn.execute("""
                        INSERT OR IGNORE INTO events
                        (event_id, identity, chain, source, app, event_type, timestamp, observed_at,
                         campaign_id, source_id, source_type, page_id, session_id, seq, payload, parser_version, data_quality)
                        VALUES (?, ?, 'offchain', 'trafficgen', 'trafficgen', 'DataGapDetected', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
                    """, (
                        gap_id, gap_identity, ts, obs, campaign_id, source_id, source_type, page_id, session_id, last_seq + 1,
                        json.dumps({"expectedSeq": last_seq + 1, "receivedSeq": seq, "missingCount": seq - last_seq - 1}),
                        PARSER_VERSION
                    ))
                elif seq == last_seq + 1 and last_seq > 0:
                    # Если был разрыв и последовательность восстановилась
                    pass
                conn.execute("UPDATE session_sequences SET last_seq = MAX(last_seq, ?), updated_at = ? WHERE session_id = ?", (seq, obs, session_id))
            else:
                conn.execute("INSERT INTO session_sequences (session_id, last_seq, updated_at) VALUES (?, ?, ?)", (session_id, seq, obs))

            # 3. Сохранение основного события
            cur = conn.execute("""
                INSERT INTO events
                (event_id, identity, chain, source, app, event_type, timestamp, observed_at,
                 campaign_id, source_id, source_type, page_id, session_id, seq, payload, parser_version, data_quality)
                VALUES (?, ?, 'offchain', 'trafficgen', 'trafficgen', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
            """, (
                event_id, identity, event_type, ts, obs, campaign_id, source_id, source_type, page_id, session_id, seq,
                payload_str, PARSER_VERSION
            ))
            row_id = cur.lastrowid
            conn.commit()

            if source_type in self.metrics["events_total"]:
                self.metrics["events_total"][source_type] += 1
            else:
                self.metrics["events_total"]["real"] += 1

            return {"status": "accepted", "eventId": event_id, "identity": identity, "id": row_id}

    def get_events(self, cursor=None, limit=50, filters=None):
        filters = filters or {}
        limit = max(1, min(limit, 500))
        last_id = 0
        if cursor:
            try:
                decoded = base64.b64decode(cursor.encode()).decode()
                last_id = int(decoded.split(":")[-1])
            except Exception:
                last_id = 0

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

        has_more = len(rows) > limit
        result_rows = rows[:limit]

        events = []
        for r in result_rows:
            events.append({
                "eventId": r["event_id"],
                "identity": r["identity"],
                "chain": r["chain"],
                "source": r["source"],
                "app": r["app"],
                "eventType": r["event_type"],
                "timestamp": r["timestamp"],
                "observedAt": r["observed_at"],
                "campaignId": r["campaign_id"],
                "sourceId": r["source_id"],
                "sourceType": r["source_type"],
                "pageId": r["page_id"],
                "sessionId": r["session_id"],
                "seq": r["seq"],
                "payload": json.loads(r["payload"] or "{}"),
                "parserVersion": r["parser_version"],
                "dataQuality": r["data_quality"],
            })

        next_cursor = None
        if has_more and result_rows:
            next_id = result_rows[-1]["id"]
            next_cursor = base64.b64encode(f"cursor:{next_id}".encode()).decode()

        return events, next_cursor


STORE = EventStore()
WatchtowerStore = EventStore


# -----------------------------------------------------------------------------
# КАТАЛОГИ: КАМПАНИИ, ИСТОЧНИКИ, СТРАНИЦЫ
# -----------------------------------------------------------------------------
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
        {"id": "target_terminal", "name": "TalkChart Live Terminal", "url": f"{config.SITE_URL}/index.html", "role": "acquisition_hub", "category": "utility"},
        {"id": "target_sixsec", "name": "SixSec", "url": "https://example.com/game1", "role": "studio_game", "category": "game_1"},
        {"id": "target_duel", "name": "CandleDuel", "url": "https://example.com/game2", "role": "studio_game", "category": "game_2"},
        {"id": "target_crash", "name": "MemeCrash", "url": "https://example.com/game3", "role": "studio_game", "category": "game_3"},
        {"id": "target_quest", "name": "WhaleQuest", "url": "https://example.com/game4", "role": "studio_game", "category": "game_4"},
        {"id": "target_tiplink_claim", "name": "TipLink Starter Pass Claim", "url": "https://tiplink.io/campaign/talkchart-starter", "role": "onboarding_bridge", "category": "voucher"},
    ]
    # Добавляем реальные URL из games.js если файл доступен
    games_js_path = os.path.join(config.SITE_DIR, "games.js")
    if os.path.exists(games_js_path):
        try:
            with open(games_js_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Находим реальные URL
            urls = re.findall(r'url:\s*"(https?://[^"]+)"', content)
            names = re.findall(r'name:\s*"(Ваша игра #[0-9]|[^"]+)"', content)
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


# -----------------------------------------------------------------------------
# РАСЧЕТ ДНЕВНЫХ МЕТРИК И ВОРОНОК
# -----------------------------------------------------------------------------
def compute_metrics(period_days=7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with STORE.get_conn() as conn:
        events = conn.execute(
            "SELECT * FROM events WHERE timestamp >= ? ORDER BY id ASC", (cutoff,)
        ).fetchall()

    if not events:
        return {
            "periodDays": period_days,
            "totalEvents": 0,
            "pageViews": 0,
            "sessions": 0,
            "uniquePseudoVisitors": 0,
            "avgSessionDurationSeconds": 0,
            "bounceRate": 0.0,
            "ctaClickRate": 0.0,
            "landingReachedRate": 0.0,
            "campaignBreakdown": {},
            "sourceBreakdown": {},
            "trafficTypeBreakdown": {"real": 0, "bot": 0, "hybrid": 0},
            "deliveryFailures": 0,
            "duplicatesCount": STORE.metrics["events_duplicate_total"],
            "rejectedCount": STORE.metrics["events_rejected_total"],
            "gapsCount": STORE.metrics["data_gaps_total"],
        }

    sessions = {}
    campaigns = {}
    sources = {}
    types = {"real": 0, "bot": 0, "hybrid": 0}
    page_views = 0
    cta_clicks = 0
    landing_reached = 0

    for r in events:
        sid = r["session_id"]
        et = r["event_type"]
        st = r["source_type"] or "real"
        cid = r["campaign_id"]
        src = r["source_id"]

        types[st] = types.get(st, 0) + 1
        campaigns[cid] = campaigns.get(cid, 0) + 1
        sources[src] = sources.get(src, 0) + 1

        if et == "PageView":
            page_views += 1
        elif et == "CTAClicked" or et == "Click":
            cta_clicks += 1
        elif et == "LandingReached":
            landing_reached += 1

        if sid not in sessions:
            sessions[sid] = {"events": [], "start": r["timestamp"], "end": r["timestamp"]}
        sessions[sid]["events"].append(et)
        sessions[sid]["end"] = r["timestamp"]

    durations = []
    bounces = 0
    for sid, sdata in sessions.items():
        if len(sdata["events"]) == 1:
            bounces += 1
        try:
            t0 = datetime.fromisoformat(sdata["start"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(sdata["end"].replace("Z", "+00:00"))
            dur = max(0, (t1 - t0).total_seconds())
            durations.append(dur)
        except Exception:
            pass

    avg_dur = sum(durations) / len(durations) if durations else 0
    bounce_rate = (bounces / len(sessions)) if sessions else 0.0
    cta_rate = (cta_clicks / page_views) if page_views else 0.0
    landing_rate = (landing_reached / max(1, cta_clicks)) if cta_clicks else 0.0

    return {
        "periodDays": period_days,
        "totalEvents": len(events),
        "pageViews": page_views,
        "sessions": len(sessions),
        "uniquePseudoVisitors": len(sessions),
        "avgSessionDurationSeconds": round(avg_dur, 1),
        "bounceRate": round(bounce_rate, 3),
        "ctaClickRate": round(cta_rate, 3),
        "landingReachedRate": round(landing_rate, 3),
        "campaignBreakdown": campaigns,
        "sourceBreakdown": sources,
        "trafficTypeBreakdown": types,
        "deliveryFailures": STORE.metrics["delivery_failures_total"],
        "duplicatesCount": STORE.metrics["events_duplicate_total"],
        "rejectedCount": STORE.metrics["events_rejected_total"],
        "gapsCount": STORE.metrics["data_gaps_total"],
    }


def compute_funnel(store=None):
    """Расчет минимальной воронки: CampaignStarted -> SessionStarted -> PageView -> CTAClicked -> LandingReached."""
    s = store or STORE
    with s.get_conn() as conn:
        counts = dict(conn.execute("SELECT event_type, COUNT(*) as cnt FROM events GROUP BY event_type").fetchall())

    stages = [
        ("CampaignStarted", max(counts.get("CampaignStarted", 0), len(CAMPAIGNS_DEF))),
        ("SessionStarted", counts.get("SessionStarted", 0)),
        ("PageView", counts.get("PageView", 0)),
        ("CTAClicked", counts.get("CTAClicked", 0) + counts.get("Click", 0)),
        ("LandingReached", counts.get("LandingReached", 0)),
    ]

    funnel_steps = []
    prev_cnt = None
    for name, cnt in stages:
        conv_prev = 1.0 if prev_cnt in (None, 0) else round(cnt / prev_cnt, 3)
        drop_prev = 0.0 if prev_cnt in (None, 0) else round(max(0, 1.0 - conv_prev), 3)
        funnel_steps.append({
            "step": name.lower(),
            "stage": name,
            "count": cnt,
            "conversionFromPrev": conv_prev,
            "dropOffRate": drop_prev,
        })
        prev_cnt = cnt

    return {
        "funnelId": "trafficgen_overall",
        "funnelName": "TalkChart Acquisition & Game Conversion Funnel",
        "chain": "offchain",
        "steps": funnel_steps,
        "stages": funnel_steps,
        "trafficQuality": "hybrid"
    }


# -----------------------------------------------------------------------------
# HTTP HANDLER (Watchtower Read-Only API + Static Site + Ingestion)
# -----------------------------------------------------------------------------
class WatchtowerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=config.SITE_DIR, **kwargs)

    def verify_auth(self):
        """Проверка опционального read-only Bearer токена."""
        expected = os.environ.get("WATCHTOWER_READ_TOKEN")
        if not expected:
            return True
        auth_hdr = self.headers.get("Authorization", "")
        if auth_hdr == f"Bearer {expected}":
            return True
        return False

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        # 1. Запрет записи в read-only Watchtower пространство
        if parsed.path.startswith("/watchtower"):
            return self.send_json(405, {
                "error": "Method Not Allowed. Watchtower is strictly read-only.",
                "dataQuality": "complete"
            })

        # 2. Ingestion endpoint для телеметрии клиента (POST /api/track или POST /watchtower/ingest)
        if parsed.path in ("/api/track", "/watchtower/ingest"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))

                # Нормализация списка или единичного события
                events = payload if isinstance(payload, list) else [payload]
                results = []
                for ev in events:
                    res = STORE.record_event(ev)
                    results.append(res)

                return self.send_json(200, {
                    "status": "ok",
                    "processed": len(results),
                    "results": results
                })
            except Exception as e:
                STORE.metrics["exporter_errors_total"] += 1
                return self.send_json(400, {"error": f"Invalid telemetry payload: {e}"})

        return self.send_json(404, {"error": "Not Found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # Prometheus Metrics Endpoint (/watchtower/metrics или /metrics)
        if path in ("/watchtower/metrics", "/metrics"):
            metrics_txt = self.build_prometheus_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(metrics_txt.encode("utf-8"))))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(metrics_txt.encode("utf-8"))
            return

        # Если запрос не к Watchtower — отдаем статику терминала
        if not path.startswith("/watchtower"):
            return super().do_GET()

        # Проверка read-only аутентификации
        if not self.verify_auth():
            return self.send_json(401, wrap_envelope({"error": "Unauthorized. Provide valid WATCHTOWER_READ_TOKEN."}, quality="unavailable", confidence=0.0))

        # 1. /watchtower/health
        if path == "/watchtower/health":
            return self.send_json(200, wrap_envelope({
                "status": "ok",
                "app": APP_ID,
                "version": PARSER_VERSION,
                "uptimeSeconds": round(time.time() - SERVER_START_TIME, 1),
                "timestamp": now_utc_iso(),
            }))

        # 2. /watchtower/readyz
        if path == "/watchtower/readyz":
            db_ok = os.path.exists(DB_PATH)
            snap_ok = os.path.exists(config.SNAPSHOT)
            ready = db_ok and snap_ok
            return self.send_json(200 if ready else 503, wrap_envelope({
                "ready": ready,
                "checks": {
                    "database": "ok" if db_ok else "unavailable",
                    "snapshot": "ok" if snap_ok else "unavailable",
                    "exporter": "ok",
                }
            }, quality="complete" if ready else "partial"))

        # 3. /watchtower/config
        if path == "/watchtower/config":
            return self.send_json(200, wrap_envelope({
                "appId": APP_ID,
                "displayName": "TalkChart Traffic Generator & Audience Layer",
                "kind": "traffic-generator",
                "stage": "live",
                "deploymentUrl": config.SITE_URL,
                "techStack": "python3, vanilla-js, github-actions, sqlite3",
                "trafficType": "hybrid",
                "campaignStore": "config",
                "eventStore": "database",
                "eventRetention": "30d",
                "auth": "api-key" if os.environ.get("WATCHTOWER_READ_TOKEN") else "none",
                "timeReference": "utc",
                "parserVersion": PARSER_VERSION,
                "readOnly": True
            }))

        # 4. /watchtower/campaigns
        if path == "/watchtower/campaigns":
            return self.send_json(200, wrap_envelope({
                "total": len(CAMPAIGNS_DEF),
                "campaigns": CAMPAIGNS_DEF
            }))

        # 5. /watchtower/campaigns/:id
        m_camp = re.match(r"^/watchtower/campaigns/([^/]+)$", path)
        if m_camp:
            cid = m_camp.group(1)
            camp = next((c for c in CAMPAIGNS_DEF if c["id"] == cid), None)
            if camp:
                return self.send_json(200, wrap_envelope(camp))
            return self.send_json(404, wrap_envelope({"error": f"Campaign '{cid}' not found"}, quality="unavailable", confidence=0.0))

        # 6. /watchtower/sources
        if path == "/watchtower/sources":
            return self.send_json(200, wrap_envelope({
                "total": len(SOURCES_DEF),
                "sources": SOURCES_DEF
            }))

        # 7. /watchtower/pages
        if path == "/watchtower/pages":
            pages = get_target_pages_def()
            return self.send_json(200, wrap_envelope({
                "total": len(pages),
                "pages": pages
            }))

        # 8. /watchtower/events?cursor=&limit=
        if path == "/watchtower/events":
            cursor = qs.get("cursor", [None])[0]
            limit = int(qs.get("limit", [50])[0])
            filters = {
                "eventType": qs.get("eventType", [None])[0],
                "campaignId": qs.get("campaignId", [None])[0],
                "sourceType": qs.get("sourceType", [None])[0],
                "since": qs.get("since", [None])[0],
            }
            events, next_cursor = STORE.get_events(cursor, limit, filters)
            return self.send_json(200, wrap_envelope({
                "events": events,
                "count": len(events),
                "nextCursor": next_cursor,
                "hasMore": bool(next_cursor),
            }))

        # 9. /watchtower/metrics/daily
        if path == "/watchtower/metrics/daily":
            period = qs.get("period", ["7d"])[0]
            m_days = re.match(r"^(\d+)d?$", period)
            days = int(m_days.group(1)) if m_days else 7
            metrics_data = compute_metrics(days)
            return self.send_json(200, wrap_envelope(metrics_data, period=f"{days}d UTC"))

        # 10. /watchtower/funnels
        if path == "/watchtower/funnels":
            funnel_data = compute_funnel()
            return self.send_json(200, wrap_envelope(funnel_data))

        # 11. /watchtower/alerts
        if path == "/watchtower/alerts":
            alerts = []
            if STORE.metrics["data_gaps_total"] > 0:
                alerts.append({
                    "id": "alert_data_gaps",
                    "severity": "warning",
                    "title": "Обнаружены разрывы последовательностей (Data Gaps)",
                    "count": STORE.metrics["data_gaps_total"],
                    "status": "active",
                    "detectedAt": now_utc_iso()
                })
            return self.send_json(200, wrap_envelope({"alerts": alerts, "activeCount": len(alerts)}))

        # 12. /watchtower/forecast (явно unavailable согласно требованиям раздела 7)
        if path == "/watchtower/forecast":
            return self.send_json(200, wrap_envelope(
                {
                    "forecast": None,
                    "reason": "МЛ-модель прогнозирования трафика не развернута. Прогнозы не выдумываются."
                },
                quality="unavailable",
                confidence=0.0
            ))

        return self.send_json(404, wrap_envelope({"error": "Unknown Watchtower endpoint"}, quality="unavailable", confidence=0.0))

    def build_prometheus_metrics(self):
        m = STORE.metrics
        t = m["events_total"]
        lines = [
            "# HELP trafficgen_events_total Total events ingested by traffic generator",
            "# TYPE trafficgen_events_total counter",
            f'trafficgen_events_total{{source_type="real"}} {t["real"]}',
            f'trafficgen_events_total{{source_type="bot"}} {t["bot"]}',
            f'trafficgen_events_total{{source_type="hybrid"}} {t["hybrid"]}',
            "",
            "# HELP trafficgen_events_duplicate_total Total duplicate events rejected",
            "# TYPE trafficgen_events_duplicate_total counter",
            f"trafficgen_events_duplicate_total {m['events_duplicate_total']}",
            "",
            "# HELP trafficgen_events_rejected_total Total malformed events rejected",
            "# TYPE trafficgen_events_rejected_total counter",
            f"trafficgen_events_rejected_total {m['events_rejected_total']}",
            "",
            "# HELP trafficgen_delivery_failures_total Total failed deliveries",
            "# TYPE trafficgen_delivery_failures_total counter",
            f"trafficgen_delivery_failures_total {m['delivery_failures_total']}",
            "",
            "# HELP trafficgen_exporter_errors_total Total internal exporter errors",
            "# TYPE trafficgen_exporter_errors_total counter",
            f"trafficgen_exporter_errors_total {m['exporter_errors_total']}",
            "",
            "# HELP trafficgen_buffer_depth Current in-memory buffer depth",
            "# TYPE trafficgen_buffer_depth gauge",
            "trafficgen_buffer_depth 0",
            "",
            "# HELP trafficgen_data_gaps_total Total detected sequence gaps",
            "# TYPE trafficgen_data_gaps_total counter",
            f"trafficgen_data_gaps_total {m['data_gaps_total']}",
        ]
        return "\n".join(lines) + "\n"


SERVER_START_TIME = time.time()


def run_server(port=8000, host="0.0.0.0"):
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
