#!/usr/bin/env python3
"""Шаг 1 фабрики: живые данные GeckoTerminal → snapshot.json + registry.json.

registry.json — реестр всех пулов, которые когда-либо видели (long-tail SEO):
пул выпадает из trending, но его страница остаётся и обновляется при появлении.

Запуск:
  python3 fetch_data.py            # живое обновление (нужен интернет)
  python3 fetch_data.py --offline  # бутстрап реестра из текущего snapshot
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

HEADERS = {"User-Agent": "talkchart-factory/0.2", "Accept": "application/json"}


def get(url, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def fetch_trending():
    j = get(f"{config.GT}/networks/{config.NETWORK}/trending_pools?page=1")
    pools = []
    for d in j.get("data", []):
        a = d.get("attributes", {})
        rel = d.get("relationships", {})
        name = a.get("name", "? / ?")
        pools.append({
            "address": a.get("address"),
            "name": name,
            "base_symbol": name.split(" / ")[0],
            "base_name": name.split(" / ")[0],
            "base_address": (rel.get("base_token", {}).get("data", {}).get("id", "_").split("_")[1]
                             if rel.get("base_token", {}).get("data") else None),
            "dex": (rel.get("dex", {}).get("data", {}) or {}).get("id", "unknown"),
            "price_usd": _f(a.get("base_token_price_usd")),
            "created_at": a.get("pool_created_at"),
            "fdv_usd": _f(a.get("fdv_usd")),
            "reserve_usd": _f(a.get("reserve_in_usd")),
            "volume_h24": _f((a.get("volume_usd") or {}).get("h24")),
            "change": {k: _f(v) for k, v in (a.get("price_change_percentage") or {}).items()
                       if k in ("h1", "h6", "h24")},
            "tx_h1": {"buys": ((a.get("transactions") or {}).get("h1") or {}).get("buys", 0),
                      "sells": ((a.get("transactions") or {}).get("h1") or {}).get("sells", 0)},
        })
    return [p for p in pools if p["address"]]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_ohlcv(address):
    url = f"{config.GT}/networks/{config.NETWORK}/pools/{address}/ohlcv/hour?aggregate=1&limit=48"
    try:
        j = get(url, retries=1)
        lst = j["data"]["attributes"]["ohlcv_list"]
        return [[t, o, h, l, c, v] for t, o, h, l, c, v in lst]  # новые -> старые (как в API)
    except Exception as e:  # noqa: BLE001 — один пул без свечей не роняет прогон
        print(f"  ! ohlcv {address[:8]}…: {e}", file=sys.stderr)
        return None


def fetch_whales(address):
    """Китовый радар: крупные сделки пула за последние сутки.

    Схема API (проверено 2026-09-21): attributes.kind = buy|sell,
    attributes.volume_in_usd, attributes.block_timestamp (ISO),
    attributes.tx_from_address.
    """
    url = f"{config.GT}/networks/{config.NETWORK}/pools/{address}/trades?limit=100"
    now = datetime.now(timezone.utc)
    try:
        j = get(url, retries=1)
    except Exception as e:  # noqa: BLE001
        print(f"  ! trades {address[:8]}…: {e}", file=sys.stderr)
        return None
    out = []
    for d in j.get("data", []):
        a = d.get("attributes", {}) or {}
        usd = _f(a.get("volume_in_usd"))
        if usd is None or usd < config.WHALE_MIN_USD:
            continue
        ts = a.get("block_timestamp")
        hours_ago = None
        try:
            hours_ago = max(0.0, (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 3600)
        except (TypeError, ValueError):
            pass
        if hours_ago is not None and hours_ago > 24:
            continue
        out.append({
            "kind": a.get("kind") or "?",
            "usd": round(usd, 2),
            "hours_ago": round(hours_ago, 1) if hours_ago is not None else None,
            "addr8": (a.get("tx_from_address") or "?")[:8],
        })
    out.sort(key=lambda w: w["usd"], reverse=True)
    return out[:5]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def merge_registry(pools):
    reg = load_json(config.REGISTRY, {"pools": {}})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for p in pools:
        addr = p["address"]
        prev = reg["pools"].get(addr, {})
        merged = {**prev, **p}
        merged["first_seen"] = prev.get("first_seen", now)
        merged["last_seen"] = now
        reg["pools"][addr] = merged
    # cap: держим REGISTRY_CAP самых свежих по last_seen
    items = sorted(reg["pools"].items(), key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
    reg["pools"] = dict(items[:config.REGISTRY_CAP])
    reg["updated_at"] = now
    save_json(config.REGISTRY, reg)
    return len(reg["pools"])


def main():
    offline = "--offline" in sys.argv
    if offline:
        snap = load_json(config.SNAPSHOT, None)
        if not snap:
            print("нет snapshot.json — офлайн-бутстрап невозможен", file=sys.stderr)
            sys.exit(1)
        n = merge_registry(snap.get("pools", []))
        print(f"OFFLINE: реестр забутстраплен из snapshot, пулов: {n}")
        return

    print("fetch: trending pools…")
    pools = fetch_trending()
    print(f"  получено {len(pools)} пулов")
    whale_targets = {p["address"] for p in
                     sorted(pools, key=lambda p: abs((p.get("change") or {}).get("h24") or 0),
                            reverse=True)[:config.WHALE_POOLS_N]}
    for i, p in enumerate(pools):
        ohlcv = fetch_ohlcv(p["address"])
        if ohlcv:
            p["ohlcv_h1"] = ohlcv
        if p["address"] in whale_targets:
            whales = fetch_whales(p["address"])
            if whales:
                p["whales"] = whales
        if (i + 1) % 5 == 0:
            print(f"  ohlcv+trades: {i + 1}/{len(pools)}")
        time.sleep(config.GT_RATE_SLEEP)

    save_json(config.SNAPSHOT, {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "network": config.NETWORK,
        "source": "api.geckoterminal.com (live factory run)",
        "pools": pools,
    })
    n = merge_registry(pools)
    with_candles = sum(1 for p in pools if p.get("ohlcv_h1"))
    print(f"OK: snapshot {len(pools)} пулов ({with_candles} со свечами), реестр: {n}")


if __name__ == "__main__":
    main()
