#!/usr/bin/env python3
"""Шаг 4 фабрики: дайджест дня — готовый текст для постов (X/каналы).

Выход:
  site/digests/<YYYY-MM-DD>.md   — полная версия (5 сюжетов + ссылки)
  site/digests/<YYYY-MM-DD>.txt  — короткий пост, готов к публикации

Публикация в соцсети — через официальные API или вручную из очереди
(правило: только собственные каналы, никакой серой автоматизации).
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from narrative import fmt_pct, fmt_usd, headline, narrative


def main():
    with open(config.SNAPSHOT) as f:
        snap = json.load(f)
    pools = snap.get("pools", [])
    if not pools:
        print("snapshot пуст", file=sys.stderr)
        sys.exit(1)

    ranked = sorted(pools, key=lambda p: abs((p.get("change") or {}).get("h24") or 0), reverse=True)[:5]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    md = [f"# Solana · история дня · {day} ({ts})", ""]
    txt = [f"Solana · история дня · {day}"]
    for i, p in enumerate(ranked, 1):
        c24 = (p.get("change") or {}).get("h24") or 0
        text, flags = narrative(p)
        addr = p.get("address")
        md.append(f"## {i}. {p.get('base_symbol')} {fmt_pct(c24)} — {p.get('name')}")
        md.append(text)
        if flags:
            md.append("**Флаги:** " + ", ".join(flags))
        md.append(f"Vol 24ч: {fmt_usd(p.get('volume_h24'))} · Ликвидность: {fmt_usd(p.get('reserve_usd'))} · FDV: {fmt_usd(p.get('fdv_usd'))}")
        md.append(f"[Живой чарт]({config.SITE_URL}/index.html#pool={addr}) · [страница актива]({config.SITE_URL}/pools/{addr}.html)")
        md.append("")
        txt.append(f"{i}. {p.get('base_symbol')} {fmt_pct(c24)} — {headline(p)}")
    md.append(f"---\n*{config.WATERMARK} · {config.SITE_URL}*")
    txt.append("")
    txt.append("Графики, которые разговаривают ↓")
    txt.append(config.SITE_URL)

    os.makedirs(config.DIGESTS_DIR, exist_ok=True)
    with open(os.path.join(config.DIGESTS_DIR, f"{day}.md"), "w") as f:
        f.write("\n".join(md))
    with open(os.path.join(config.DIGESTS_DIR, f"{day}.txt"), "w") as f:
        f.write("\n".join(txt))
    print(f"OK: дайджест {day} (md + txt), сюжетов: {len(ranked)}")


if __name__ == "__main__":
    main()
