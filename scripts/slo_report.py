#!/usr/bin/env python3
"""Отчёт по SLO trafficgen (W4, o-02).

Считает только то, что измеримо фактами, и явно помечает неизмеренное:

| SLO                          | Источник                                              |
|------------------------------|-------------------------------------------------------|
| availability ≥ 99.9%/мес     | события `ExporterHealth` (status) + uptime процесса    |
| свежесть данных ≤ 5 мин      | `lagSeconds` из последнего `ExporterHealth`            |
| ingest p99 ≤ 2 с             | БД: `observedAt - timestamp` по событиям окна          |
| API read p95 ≤ 300 мс        | таблица `latency_samples` (замеры в самом обработчике) |
| целостность: 0 потерь        | счётчики accepted/rejected/duplicate + DLQ             |

Ни одна цифра не «придумана для отчёта»: если источник недоступен, поле
получает `null` и `status: "unknown"`, а не 0 и не 100%.

Использование
-------------
    python3 scripts/slo_report.py                     # онлайн: живой экспортёр
    python3 scripts/slo_report.py --offline           # только по БД
    python3 scripts/slo_report.py --days 30
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))
import config  # noqa: E402

DB_PATH = os.path.join(config.DATA_DIR, "watchtower.db")
DEFAULT_BASE = os.environ.get("TRAFFICGEN_API_BASE_URL", "http://127.0.0.1:8000")
JSON_PATH = os.path.join(REPO_ROOT, "reports", "slo-trafficgen.json")
MD_PATH = os.path.join(REPO_ROOT, "reports", "slo-trafficgen.md")

TARGETS = {
    "availability": 99.9,
    "freshnessSeconds": 300,
    "ingestP99Ms": 2000,
    "apiP95Ms": 300,
    "apiP99Ms": 1000,
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def http_json(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q / 100 * (len(ordered) - 1))))
    return round(ordered[idx], 2)


def probe_online(base):
    health = http_json(f"{base}/watchtower/health")
    quality = http_json(f"{base}/watchtower/quality")
    events = http_json(f"{base}/watchtower/events?eventType=ExporterHealth&limit=500")
    return {
        "health": (health or {}).get("data"),
        "quality": (quality or {}).get("data"),
        "healthEvents": ((events or {}).get("data") or {}).get("events", []),
        "reachable": health is not None,
    }


def offline_stats(db_path, days):
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "SELECT timestamp, observed_at FROM events WHERE timestamp >= ? LIMIT 20000",
            (cutoff,)).fetchall()
        lags = []
        backfill = 0
        skewed = 0
        # Backfill (старые события, догруженные позже) — это не задержка
        # живого приёма: иначе пара历史事件 из 2020 года даст p99 в неделях.
        # Их считаем отдельно и в процентили не берём.
        BACKFILL_THRESHOLD_MS = 3_600_000  # 1 час
        for ts, obs in rows:
            try:
                a = datetime.fromisoformat(str(obs).replace("Z", "+00:00"))
                b = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                lag = (a - b).total_seconds() * 1000.0
            except Exception:
                continue
            if lag < 0:
                skewed += 1
                continue
            if lag > BACKFILL_THRESHOLD_MS:
                backfill += 1
                continue
            lags.append(lag)
        health_rows = conn.execute(
            "SELECT payload FROM events WHERE event_type='ExporterHealth' "
            "ORDER BY id DESC LIMIT 500").fetchall()
        statuses = []
        freshness = []
        for (payload,) in health_rows:
            try:
                p = json.loads(payload)
            except Exception:
                continue
            if p.get("status"):
                statuses.append(p["status"])
            if isinstance(p.get("lagSeconds"), (int, float)):
                freshness.append(float(p["lagSeconds"]))
        lat = conn.execute(
            "SELECT route, ms FROM latency_samples WHERE ts >= ?", (cutoff,)).fetchall()
        latencies = {}
        for route, ms in lat:
            latencies.setdefault(route, []).append(ms)
        rejected = conn.execute("SELECT COUNT(*) FROM rejected_events").fetchone()[0]
        return {
            "ingestLagMs": lags,
            "backfillSamples": backfill,
            "clockSkewSamples": skewed,
            "healthStatuses": statuses,
            "freshness": freshness,
            "latencies": latencies,
            "deadLetterTotal": rejected,
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def build_report(base=DEFAULT_BASE, days=30, offline=False):
    online = {} if offline else probe_online(base)
    db_stats = offline_stats(DB_PATH, days)

    # --- availability -------------------------------------------------------
    statuses = (db_stats.get("healthStatuses")
                or [((e.get("payload") or {}).get("status")) for e in online.get("healthEvents", [])]
                or [])
    statuses = [s for s in statuses if s]
    if statuses:
        healthy = sum(1 for s in statuses if s != "unhealthy")
        availability = round(100.0 * healthy / len(statuses), 3)
        availability_source = f"{len(statuses)} событий ExporterHealth за окно"
    else:
        availability, availability_source = None, "нет событий ExporterHealth — измерить нельзя"

    uptime = (online.get("health") or {}).get("uptimeSeconds")
    if availability is None and uptime:
        availability = round(100.0 * min(1.0, uptime / (days * 86400)), 3)
        availability_source = f"uptime процесса {uptime} с против окна {days} дн."

    # --- свежесть ------------------------------------------------------------
    freshness_values = db_stats.get("freshness") or [
        (e.get("payload") or {}).get("lagSeconds") for e in online.get("healthEvents", [])]
    freshness_values = [v for v in freshness_values if isinstance(v, (int, float))]
    freshness = round(max(freshness_values), 1) if freshness_values else None

    # --- ingest latency -------------------------------------------------------
    ingest = db_stats.get("ingestLagMs") or []
    ingest_p99 = percentile(ingest, 99)
    ingest_p95 = percentile(ingest, 95)

    # --- API latency ----------------------------------------------------------
    if offline:
        latencies = db_stats.get("latencies", {})
        api_values = [v for vals in latencies.values() for v in vals]
        api_p95 = percentile(api_values, 95)
        api_p99 = percentile(api_values, 99)
        track_values = latencies.get("track", [])
    else:
        lat = (online.get("quality") or {}).get("latency", {}) or {}
        api_p95 = (lat.get("api") or {}).get("p95")
        api_p99 = (lat.get("api") or {}).get("p99")
        track_values = []
        track_p95 = (lat.get("track") or {}).get("p95")
    track_p95 = percentile(track_values, 95) if track_values else (
        locals().get("track_p95") if not offline else None)

    dlq = None
    if not offline:
        dlq = ((online.get("quality") or {}).get("deadLetter") or {}).get("total")
    if dlq is None:
        dlq = db_stats.get("deadLetterTotal")

    def verdict(name, value, target, higher_is_worse=True):
        if value is None:
            return {"status": "unknown", "value": None, "target": target}
        ok = (value <= target) if higher_is_worse else (value >= target)
        return {"status": "ok" if ok else "breach", "value": value, "target": target}

    slos = {
        "availabilityPercent": verdict("availability", availability, TARGETS["availability"],
                                       higher_is_worse=False),
        "freshnessSeconds": verdict("freshness", freshness, TARGETS["freshnessSeconds"]),
        "ingestP99Ms": verdict("ingest", ingest_p99, TARGETS["ingestP99Ms"]),
        "apiP95Ms": verdict("api95", api_p95, TARGETS["apiP95Ms"]),
        "apiP99Ms": verdict("api99", api_p99, TARGETS["apiP99Ms"]),
    }

    return {
        "generatedAt": now_utc_iso(),
        "windowDays": days,
        "mode": "offline" if offline else ("online" if online.get("reachable") else "degraded"),
        "target": base,
        "slos": slos,
        "sources": {
            "availability": availability_source,
            "ingestSamples": len(ingest),
            "backfillSamples": db_stats.get("backfillSamples"),
            "clockSkewSamples": db_stats.get("clockSkewSamples"),
            "deadLetterTotal": dlq,
            "exporterUptimeSeconds": uptime,
        },
        "allGreen": all(s["status"] == "ok" for s in slos.values()),
    }


def to_markdown(report):
    lines = [
        "# SLO trafficgen — фактический отчёт",
        "",
        f"Сгенерировано: {report['generatedAt']} · окно: {report['windowDays']} дн. · "
        f"режим: {report['mode']}",
        "",
        "| SLO | Цель | Факт | Статус |",
        "|---|---|---|---|",
    ]
    labels = {
        "availabilityPercent": ("Availability экспортёра", "%"),
        "freshnessSeconds": ("Свежесть данных", "с"),
        "ingestP99Ms": ("Ingest p99", "мс"),
        "apiP95Ms": ("API read p95", "мс"),
        "apiP99Ms": ("API read p99", "мс"),
    }
    for key, (label, unit) in labels.items():
        s = report["slos"][key]
        value = "—" if s["value"] is None else f"{s['value']} {unit}"
        mark = {"ok": "✅", "breach": "❌", "unknown": "⚠️ неизмерено"}[s["status"]]
        lines.append(f"| {label} | {s['target']} {unit} | {value} | {mark} |")
    lines += [
        "",
        f"Источник availability: {report['sources']['availability']}.",
        f"Выборка ingest: {report['sources']['ingredients'] if False else report['sources']['ingestSamples']} событий.",
        f"DLQ (отклонённые): {report['sources']['deadLetterTotal']}.",
        "",
        "Поле со статусом «⚠️ неизмерено» означает отсутствие источника данных,",
        "а не нулевое отклонение: подменять неизмеренное нулём — значит врать.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Отчёт по SLO trafficgen")
    ap.add_argument("--url", default=DEFAULT_BASE)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args(argv)

    report = build_report(args.url, args.days, args.offline)
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(MD_PATH, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nотчёты: {os.path.relpath(JSON_PATH, REPO_ROOT)}, "
          f"{os.path.relpath(MD_PATH, REPO_ROOT)}")
    return 0 if report["allGreen"] else 1


if __name__ == "__main__":
    sys.exit(main())
