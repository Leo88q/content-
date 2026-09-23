#!/usr/bin/env python3
"""Юнит-экономика привлечения (o-03): стоимость 1 000 визитов.

Считает только то, что можно посчитать: визиты берутся из метрик экспортёра
( Sessions / PageView за окно ), а стоимость — из переменных окружения, потому
что цены инфраструктуры известны владельцу, а не коду.

Если стоимость не задана, отчёт не подставляет ноль и не угадывает: поле
получает `null` и `unavailable` с причиной. Цифра, которую нельзя проверить,
хуже отсутствия цифры.

Переменные (все необязательные, по месяцам):
    TRAFFICGEN_COST_INFRA       — сервер/хостинг
    TRAFFICGEN_COST_CI          — минуты CI и фабрики контента
    TRAFFICGEN_COST_TOOLS       — подписки (ИИ, парсинг, прокси)
    TRAFFICGEN_COST_TEAM        — доля времени команды
    TRAFFICGEN_COST_CURRENCY    — валюта (по умолчанию EUR)

Использование
-------------
    python3 scripts/unit_economics.py                       # по БД
    python3 scripts/unit_economics.py --days 30 --url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))
import config  # noqa: E402
import watchtower_exporter as we  # noqa: E402

OUT_PATH = os.path.join(REPO_ROOT, "reports", "unit-economics.json")
DEFAULT_BASE = os.environ.get("TRAFFICGEN_API_BASE_URL", "")

COST_KEYS = {
    "infrastructure": "TRAFFICGEN_COST_INFRA",
    "ciAndFactory": "TRAFFICGEN_COST_CI",
    "toolsAndSubscriptions": "TRAFFICGEN_COST_TOOLS",
    "teamShare": "TRAFFICGEN_COST_TEAM",
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_costs():
    costs, missing = {}, []
    for label, env in COST_KEYS.items():
        raw = os.environ.get(env)
        if raw in (None, ""):
            missing.append(env)
            costs[label] = None
            continue
        try:
            costs[label] = float(raw)
        except ValueError:
            missing.append(f"{env} (не число)")
            costs[label] = None
    return costs, missing


def fetch_metrics(base, days):
    if base:
        try:
            with urllib.request.urlopen(f"{base}/watchtower/metrics/daily?period={days}d",
                                        timeout=10) as r:
                env = json.loads(r.read().decode())
                return env.get("data"), "live"
        except Exception:
            pass
    return we.compute_metrics(days, we.STORE), "offline"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Юнит-экономика привлечения")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--url", default=DEFAULT_BASE)
    args = ap.parse_args(argv)

    metrics, mode = fetch_metrics(args.url, args.days)
    totals = metrics.get("totals") or {}
    sessions = int(totals.get("sessions") or 0)
    page_views = int(totals.get("pageViews") or 0)
    traffic = totals.get("trafficType") or {}

    costs, missing = read_costs()
    known = [v for v in costs.values() if v is not None]
    total_known = sum(known) if known else None

    def per_1000(value, count):
        if value is None or not count:
            return None
        return round(1000.0 * value / count, 4)

    report = {
        "generatedAt": now_utc_iso(),
        "mode": mode,
        "windowDays": args.days,
        "traffic": {
            "sessions": sessions,
            "pageViews": page_views,
            "byType": traffic,
        },
        "costsMonthly": {**costs, "total": total_known,
                         "currency": os.environ.get("TRAFFICGEN_COST_CURRENCY", "EUR")},
        "unit": {
            "costPer1000Sessions": per_1000(total_known, sessions),
            "costPer1000PageViews": per_1000(total_known, page_views),
        },
        "unavailable": [],
        "sources": "визиты — метрики экспортёра (реальные события); "
                   "стоимость — переменные окружения владельца",
    }
    if missing:
        report["unavailable"].append({
            "metric": "costPer1000*",
            "reason": "не заданы переменные стоимости: " + ", ".join(missing) +
                      ". Подставлять цену «по ощущениям» не стали.",
        })
    if not sessions:
        report["unavailable"].append({
            "metric": "costPer1000Sessions",
            "reason": "за окно нет ни одной сессии — знаменатель нулевой, делить нельзя.",
        })
    report["complete"] = not report["unavailable"]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nотчёт: {os.path.relpath(OUT_PATH, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
