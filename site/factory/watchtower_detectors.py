#!/usr/bin/env python3
"""Детекторы и фоновые эмиттеры trafficgen.

Здесь появляются эмиттеры для семи событий, которые раньше честно числились
`unavailable`: `ExporterHealth`, `SessionAbandoned`, `BotFlagged`,
`AnomalyDetected`, `TrafficError`, `EmergencyPause`, `AbuseBlocked`
(последние два — только через proposal-контроль, см. watchtower_control.py).

Принципы
--------
1. **Детектор предлагает, человек решает.** Классификатор ботов и детектор
   аномалий только *фиксируют* наблюдение и считают оценку. Блокировка
   источника или сессии — исключительно через proposal с двумя подтверждениями.
   Автоматического «забанить и забыть» здесь нет и не планируется.
2. **Порог и метод — часть события.** Каждое `AnomalyDetected`/`BotFlagged`
   несёт `threshold`, `method` и `score`: цифру без порога нельзя проверить.
3. **Каждое решение пишется в журнал** (`detector_decisions`) с возможностью
   проставить метку позже. Тогда precision/recall считаются по факту, а не
   обсуждаются на словах. Метки-прокси (`factory_pipeline` → bot и т.п.)
   помечаются явно: это не разметка людьми, а грубая прокси-разметка.
4. **Идемпотентность.** У каждого события своя детерминированная identity:
   повторный проход детектора не плодит дубли (их съест UNIQUE, но лучше не
   создавать лишней работы БД).
5. **Никакой магии в данных.** Если базовой выборки мало для вывода — детектор
   молчит, а не «додумывает» аномалию.

Все эмиттеры работают в фоновых потоках и выключаются переменной
`TRAFFICGEN_DETECTORS=0` (например, в модульных тестах).
"""
from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

APP_ID = "trafficgen"
PARSER_VERSION = "trafficgen-v1"

DETECTORS_ENABLED = os.environ.get("TRAFFICGEN_DETECTORS", "1") != "0"
HEALTH_INTERVAL = int(os.environ.get("TRAFFICGEN_HEALTH_INTERVAL_SECONDS", "300"))
ABANDON_INTERVAL = int(os.environ.get("TRAFFICGEN_ABANDON_INTERVAL_SECONDS", "120"))
SESSION_IDLE_TIMEOUT = int(os.environ.get("TRAFFICGEN_SESSION_IDLE_TIMEOUT_SECONDS", "1800"))
BOT_INTERVAL = int(os.environ.get("TRAFFICGEN_BOT_INTERVAL_SECONDS", "300"))
BOT_THRESHOLD = float(os.environ.get("TRAFFICGEN_BOT_THRESHOLD", "0.6"))
BOT_MIN_EVENTS = int(os.environ.get("TRAFFICGEN_BOT_MIN_EVENTS", "3"))
ANOMALY_INTERVAL = int(os.environ.get("TRAFFICGEN_ANOMALY_INTERVAL_SECONDS", "900"))
ANOMALY_SIGMA = float(os.environ.get("TRAFFICGEN_ANOMALY_SIGMA", "3.5"))
ANOMALY_MIN_BASELINE = int(os.environ.get("TRAFFICGEN_ANOMALY_MIN_BASELINE", "6"))

# События, которые означают «сессия завершена явно»
TERMINAL_EVENTS = ("SessionEnded", "SessionAbandoned")
# События, не являющиеся пользовательскими (не считаем их активностью сессии)
META_EVENTS = ("DataGapDetected", "DataGapHealed", "ExporterHealth", "RateLimited")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# Эмиссия системного события
# ---------------------------------------------------------------------------
def emit_system_event(store, event_type, payload, session, *, identity=None,
                      campaign_id="system", page_id="target_terminal",
                      source_id="watchtower_exporter", source_type="bot",
                      timestamp=None):
    """Записать событие от имени экспортёра/детектора.

    session начинается с `sess_system` — такие сессии исключаются из
    gap-detection'а и из пользовательской аналитики.
    """
    ts = timestamp or now_utc_iso()
    return store.record_event({
        "eventType": event_type,
        "campaignId": campaign_id,
        "sourceId": source_id,
        "sourceType": source_type,
        "pageId": page_id,
        "sessionId": session,
        "seq": int(time.time() * 1000),
        "identity": identity,
        "timestamp": ts,
        "observedAt": ts,
        "payload": payload,
    })


# ---------------------------------------------------------------------------
# 1. ExporterHealth — периодический self-check, отдельный от health/readyz
# ---------------------------------------------------------------------------
class HealthProbe:
    """Состояние самого экспортёра: БД, снапшот, свежесть событий, разрывы."""

    name = "exporter_health"

    def __init__(self, store, snapshot_path=None, freshness_budget=300):
        self.store = store
        self.snapshot_path = snapshot_path
        self.freshness_budget = freshness_budget

    def check_database(self):
        try:
            with self.store.get_conn() as conn:
                conn.execute(
                    "INSERT INTO metrics_state (key, value) VALUES ('health_probe', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now_utc_iso(),))
                conn.commit()
                row = conn.execute(
                    "SELECT value FROM metrics_state WHERE key='health_probe'").fetchone()
            return "ok" if row else "unavailable"
        except Exception:
            return "unavailable"

    def check_snapshot(self):
        if not self.snapshot_path or not os.path.exists(self.snapshot_path):
            return "unavailable", None
        age = time.time() - os.path.getmtime(self.snapshot_path)
        return ("ok" if age <= 24 * 3600 else "stale"), round(age, 1)

    def check_freshness(self):
        """Лаг между «сейчас» и последним пользовательским событием."""
        try:
            with self.store.get_conn() as conn:
                row = conn.execute(
                    "SELECT MAX(timestamp) AS ts FROM events WHERE is_synthetic = 0 "
                    "AND session_id NOT LIKE 'sess_system%'").fetchone()
        except Exception:
            return "unavailable", None
        if not row or not row["ts"]:
            return "no_events", None
        try:
            last = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except Exception:
            return "unavailable", None
        lag = (datetime.now(timezone.utc) - last).total_seconds()
        status = "ok" if lag <= self.freshness_budget else "stale"
        return status, round(lag, 1)

    def open_gaps(self):
        try:
            with self.store.get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM session_gaps WHERE status='open'").fetchone()
            return row["c"] if row else 0
        except Exception:
            return None

    def run(self):
        db = self.check_database()
        snap, snap_age = self.check_snapshot()
        fresh, lag = self.check_freshness()
        gaps = self.open_gaps()

        degraded = ("unavailable" in (db, snap)) or fresh == "stale"
        unhealthy = db == "unavailable"
        status = "unhealthy" if unhealthy else ("degraded" if degraded else "healthy")

        payload = {
            "status": status,
            "intervalSeconds": HEALTH_INTERVAL,
            "checks": {
                "database": db,
                "snapshot": snap,
                "snapshotAgeSeconds": snap_age,
                "eventsFreshness": fresh,
            },
            "lagSeconds": lag,
            "freshnessBudgetSeconds": self.freshness_budget,
            "openGaps": gaps,
            "detectors": {
                "enabled": DETECTORS_ENABLED,
                "botThreshold": BOT_THRESHOLD,
                "anomalySigma": ANOMALY_SIGMA,
            },
        }
        return emit_system_event(
            self.store, "ExporterHealth", payload,
            session="sess_system_health",
            identity=f"offchain:trafficgen:system:target_terminal:sess_system_health:{int(time.time())}",
        ), payload


# ---------------------------------------------------------------------------
# 2. SessionAbandoned — планировщик таймаутов сессий
# ---------------------------------------------------------------------------
class SessionSweeper:
    """Закрывает сессии, молчащие дольше SESSION_IDLE_TIMEOUT."""

    name = "session_abandon"

    def __init__(self, store, control=None):
        self.store = store
        self.control = control

    def run(self):
        cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=SESSION_IDLE_TIMEOUT)
        cutoff = iso(cutoff_dt)
        with self.store.get_conn() as conn:
            rows = [dict(r) for r in conn.execute("""
                SELECT session_id,
                       MAX(timestamp) AS last_ts,
                       COUNT(*) AS cnt,
                       MAX(campaign_id) AS campaign_id,
                       MAX(page_id) AS page_id
                FROM events
                WHERE session_id NOT LIKE 'sess_system%'
                  AND event_type NOT IN ('DataGapDetected', 'DataGapHealed')
                GROUP BY session_id
                HAVING MAX(timestamp) < ?
            """, (cutoff,)).fetchall()]
            already = {
                r["session_id"] for r in conn.execute(
                    "SELECT DISTINCT session_id FROM events "
                    "WHERE event_type IN ('SessionEnded', 'SessionAbandoned')").fetchall()
            }
            last_stages = {}
            for r in conn.execute("""
                SELECT e.session_id, e.event_type FROM events e
                INNER JOIN (SELECT session_id, MAX(id) AS mid FROM events
                            WHERE session_id NOT LIKE 'sess_system%'
                            GROUP BY session_id) m
                    ON e.id = m.mid
            """).fetchall():
                last_stages[r["session_id"]] = r["event_type"]

        emitted = []
        for r in rows:
            sid = r["session_id"]
            if sid in already:
                continue
            try:
                last_dt = datetime.fromisoformat(r["last_ts"].replace("Z", "+00:00"))
            except Exception:
                continue
            idle = (datetime.now(timezone.utc) - last_dt).total_seconds()
            payload = {
                "lastEventAt": r["last_ts"],
                "idleSeconds": round(idle, 1),
                "timeoutSeconds": SESSION_IDLE_TIMEOUT,
                "lastStage": last_stages.get(sid),
                "eventCount": r["cnt"],
            }
            res = emit_system_event(
                self.store, "SessionAbandoned", payload, session=sid,
                campaign_id=r["campaign_id"] or "system",
                page_id=r["page_id"] or "target_terminal",
                identity=f"offchain:trafficgen:{r['campaign_id'] or 'system'}:"
                         f"{r['page_id'] or 'target_terminal'}:{sid}:abandon",
                timestamp=iso(datetime.now(timezone.utc)),
            )
            if res.get("status") == "accepted":
                emitted.append(sid)
        return emitted


# ---------------------------------------------------------------------------
# 3. BotFlagged — классификатор ботов (правила + порог)
# ---------------------------------------------------------------------------
class BotClassifier:
    """Взвешенные правила. Возвращает (score, reasons); решение — по порогу.

    Признаки намеренно грубые и объяснимые: объяснимая эвристика с измеренной
    точностью полезнее «нейросети», чью ошибку нельзя ни проверить, ни оспорить.
    """

    name = "bot_classifier"

    def __init__(self, store, control=None, threshold=BOT_THRESHOLD):
        self.store = store
        self.control = control
        self.threshold = threshold

    @staticmethod
    def score(rows):
        if not rows:
            return 0.0, []
        reasons = []
        score = 0.0
        types = [r["event_type"] for r in rows]
        stamps = sorted(
            datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            for r in rows if r.get("timestamp")
        )

        if "SessionStarted" not in types:
            score += 0.20
            reasons.append("no_session_start")
        if len(rows) <= 1:
            score += 0.25
            reasons.append("single_event_session")
        if len(stamps) >= 2:
            min_delta = min(
                (stamps[i + 1] - stamps[i]).total_seconds() for i in range(len(stamps) - 1))
            if min_delta < 0.05:
                score += 0.35
                reasons.append(f"inhuman_inter_event_delta:{round(min_delta, 3)}s")
            elif min_delta < 0.25:
                score += 0.15
                reasons.append(f"suspicious_inter_event_delta:{round(min_delta, 3)}s")
        if len(stamps) >= 2 and (stamps[-1] - stamps[0]).total_seconds() == 0:
            score += 0.20
            reasons.append("zero_dwell_time")
        page_views = sum(1 for t in types if t == "PageView")
        clicks = sum(1 for t in types if t in ("Click", "CTAClicked"))
        if page_views >= 5 and clicks == 0:
            score += 0.15
            reasons.append("many_pageviews_no_clicks")
        for r in rows:
            try:
                payload = json.loads(r.get("payload") or "{}")
            except Exception:
                payload = {}
            if payload.get("automated") is True or payload.get("bot") is True:
                score += 0.50
                reasons.append("self_declared_automation")
                break
        if any(r.get("source_id") == "factory_pipeline" for r in rows):
            score += 1.00
            reasons.append("factory_pipeline_source")
        return round(min(1.0, score), 3), reasons

    def run(self):
        with self.store.get_conn() as conn:
            rows = [dict(r) for r in conn.execute("""
                SELECT * FROM events
                WHERE session_id NOT LIKE 'sess_system%' AND is_synthetic = 0
                ORDER BY id ASC
            """).fetchall()]
        sessions = {}
        for r in rows:
            sessions.setdefault(r["session_id"], []).append(r)

        flagged = []
        for sid, evs in sessions.items():
            if len(evs) < BOT_MIN_EVENTS:
                continue
            proxy_label = "bot" if any(
                e.get("source_id") == "factory_pipeline" for e in evs) else None
            score, reasons = self.score(evs)
            decision = "flagged" if score >= self.threshold else "clean"
            if self.control:
                self.control.record_decision(
                    self.name, sid, score, self.threshold, decision,
                    {"reasons": reasons, "events": len(evs), "proxyLabel": proxy_label})
                if proxy_label:
                    self.control.label_decision(self.name, sid, proxy_label)
            if decision != "flagged":
                continue
            campaign = evs[-1].get("campaign_id") or "system"
            page = evs[-1].get("page_id") or "target_terminal"
            payload = {
                "sessionId": sid,
                "score": score,
                "threshold": self.threshold,
                "method": "weighted_rules_v1",
                "reasons": reasons,
                "eventCount": len(evs),
                "proxyLabel": proxy_label,
            }
            res = emit_system_event(
                self.store, "BotFlagged", payload, session=sid,
                campaign_id=campaign, page_id=page,
                identity=f"offchain:trafficgen:{campaign}:{page}:{sid}:botflag",
            )
            if res.get("status") == "accepted":
                flagged.append({"sessionId": sid, "score": score, "reasons": reasons})
        return flagged


# ---------------------------------------------------------------------------
# 4. AnomalyDetected — робастный z-score по часовым корзинам
# ---------------------------------------------------------------------------
class AnomalyDetector:
    """Сравнивает текущий час с медианой/MAД предыдущих часов суток.

    Робастная статистика (медиана + MAD), а не среднее/σ: в трафике выбросы
    норма, и классическая σ на них залипает.
    """

    name = "anomaly_detector"

    def __init__(self, store, control=None, sigma=ANOMALY_SIGMA,
                 baseline_hours=24, min_baseline=ANOMALY_MIN_BASELINE):
        self.store = store
        self.control = control
        self.sigma = sigma
        self.baseline_hours = baseline_hours
        self.min_baseline = min_baseline

    def hourly_counts(self, scope_key, hours):
        since = iso(datetime.now(timezone.utc) - timedelta(hours=hours))
        with self.store.get_conn() as conn:
            rows = conn.execute("""
                SELECT substr(timestamp, 1, 13) AS bucket,
                       campaign_id, page_id, COUNT(*) AS cnt
                FROM events
                WHERE timestamp >= ? AND is_synthetic = 0
                  AND session_id NOT LIKE 'sess_system%'
                GROUP BY bucket, campaign_id, page_id
            """, (since,)).fetchall()
        series = {}
        for r in rows:
            key = (r["campaign_id"] or "-", r["page_id"] or "-")
            series.setdefault(key, {}).setdefault(r["bucket"], 0)
            series[key][r["bucket"]] += r["cnt"]
        return series

    @staticmethod
    def robust_z(value, baseline):
        """(z, median, mad). При вырожденном MAD считаем по межквартильному."""
        if len(baseline) < 2:
            return None, (statistics.median(baseline) if baseline else None), None
        med = statistics.median(baseline)
        mad = statistics.median([abs(v - med) for v in baseline])
        scale = mad * 1.4826 if mad else 0.0
        if not scale:
            q1 = statistics.quantiles(baseline, n=4)[0] if len(baseline) >= 4 else 0
            q3 = statistics.quantiles(baseline, n=4)[2] if len(baseline) >= 4 else 0
            scale = (q3 - q1) / 1.349 if (q3 - q1) else 0.0
        if not scale:
            # База константа (MAD = 0 и IQR = 0): классический z не определён.
            # Молчать здесь нельзя — ровно такой фон типичен для ночного
            # трафика, и всплеск на нём самый показательный. Поэтому при
            # нулевой дисперсии используем относительное отклонение от медианы,
            # масштабированное в «сигмы»: любое отклонение от константы
            # считается значимым, его величина определяет только силу сигнала.
            if value == med:
                return 0.0, med, mad
            direction = 1.0 if value > med else -1.0
            return direction * (abs(value - med) / max(1.0, abs(med))) * 10.0, med, mad
        return (value - med) / scale, med, mad

    def run(self):
        series = self.hourly_counts("events", self.baseline_hours + 1)
        current_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        detected = []

        for (cid, pid), buckets in series.items():
            baseline = [v for b, v in sorted(buckets.items()) if b < current_bucket]
            value = buckets.get(current_bucket, 0)
            if len(baseline) < self.min_baseline:
                continue
            z, med, mad = self.robust_z(value, baseline)
            if self.control:
                self.control.record_decision(
                    self.name, f"{cid}:{pid}", abs(z) if z is not None else None,
                    self.sigma,
                    "anomaly" if (z is not None and abs(z) >= self.sigma) else "normal",
                    {"value": value, "median": med, "mad": mad, "baselineHours": len(baseline)})
            if z is None or abs(z) < self.sigma:
                continue
            payload = {
                "scope": {"campaignId": cid, "pageId": pid},
                "metric": "events_per_hour",
                "value": value,
                "baselineMedian": med,
                "mad": mad,
                "sigmaScore": round(z, 3),
                "threshold": self.sigma,
                "method": "robust_zscore_mad_v1",
                "direction": "spike" if z > 0 else "drop",
                "baselineHours": len(baseline),
                "bucket": current_bucket,
            }
            res = emit_system_event(
                self.store, "AnomalyDetected", payload,
                session="sess_system_anomaly",
                campaign_id=cid if cid != "-" else "system",
                page_id=pid if pid != "-" else "target_terminal",
                identity=f"offchain:trafficgen:{cid}:{pid}:anomaly:{current_bucket}",
            )
            if res.get("status") == "accepted":
                detected.append(payload)
        return detected


# ---------------------------------------------------------------------------
# Фоновый планировщик
# ---------------------------------------------------------------------------
class DetectorScheduler:
    def __init__(self, store, control=None, snapshot_path=None):
        self.store = store
        self.control = control
        self.snapshot_path = snapshot_path
        self.threads = []
        self.stop_event = threading.Event()
        self.last_run = {}

    def _loop(self, name, interval, fn):
        while not self.stop_event.is_set():
            try:
                result = fn()
                self.last_run[name] = {"at": now_utc_iso(), "ok": True,
                                       "result": _brief(result)}
            except Exception as e:  # детектор не имеет права ронять процесс
                self.last_run[name] = {"at": now_utc_iso(), "ok": False, "error": str(e)}
            self.stop_event.wait(interval)

    def start(self):
        if not DETECTORS_ENABLED:
            return []
        health = HealthProbe(self.store, snapshot_path=self.snapshot_path)
        sweeper = SessionSweeper(self.store, self.control)
        bots = BotClassifier(self.store, self.control)
        anomalies = AnomalyDetector(self.store, self.control)

        plan = [
            ("exporter_health", HEALTH_INTERVAL, health.run),
            ("session_abandon", ABANDON_INTERVAL, sweeper.run),
            ("bot_classifier", BOT_INTERVAL, bots.run),
            ("anomaly_detector", ANOMALY_INTERVAL, anomalies.run),
        ]
        for name, interval, fn in plan:
            t = threading.Thread(target=self._loop, args=(name, interval, fn),
                                 name=f"tc-{name}", daemon=True)
            t.start()
            self.threads.append(t)
        return [name for name, _, _ in plan]

    def stop(self):
        self.stop_event.set()

    def status(self):
        return {
            "enabled": DETECTORS_ENABLED,
            "threads": [t.name for t in self.threads if t.is_alive()],
            "intervals": {
                "exporterHealthSeconds": HEALTH_INTERVAL,
                "sessionAbandonSeconds": ABANDON_INTERVAL,
                "botClassifierSeconds": BOT_INTERVAL,
                "anomalyDetectorSeconds": ANOMALY_INTERVAL,
            },
            "params": {
                "sessionIdleTimeoutSeconds": SESSION_IDLE_TIMEOUT,
                "botThreshold": BOT_THRESHOLD,
                "botMinEvents": BOT_MIN_EVENTS,
                "anomalySigma": ANOMALY_SIGMA,
                "anomalyMinBaselineHours": ANOMALY_MIN_BASELINE,
            },
            "lastRun": self.last_run,
        }


def _brief(result):
    if isinstance(result, tuple):
        return _brief(result[0])
    if isinstance(result, list):
        return {"count": len(result)}
    if isinstance(result, dict):
        return {"keys": sorted(result.keys())[:6]}
    return {"value": result}
