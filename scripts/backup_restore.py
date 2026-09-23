#!/usr/bin/env python3
"""Резервное копирование и ПРОВЕРЕННОЕ восстановление БД экспортёра (DR, o-05).

Бэкап, который ни разу не восстанавливали, — это не бэкап, а файл. Поэтому
команда `--verify` не «проверяет целостность архива», а реально поднимает
копию в отдельный файл и сравнивает её с живой базой потаблично: число строк
и контрольная сумма содержимого. Расхождение — ошибка, а не предупреждение.

Что измеряется
--------------
- `restoreSeconds` — фактическое время восстановления (RTO считается по нему);
- `rpoSeconds` — окно возможной потери: разница между моментом бэкапа и
  последним событием в БД. Если бэкап делается раз в 15 минут, RPO ≤ 15 мин
  выполнимо только при непрерывном WAL-копировании — честно показываем число.

Использование
-------------
    python3 scripts/backup_restore.py --backup
    python3 scripts/backup_restore.py --verify            # восстановить последний бэкап
    python3 scripts/backup_restore.py --report            # бэкап + проверка + отчёт
    python3 scripts/backup_restore.py --backup --out /mnt/backups

Cron (каждые 15 минут):
    */15 * * * * cd /opt/trafficgen && python3 scripts/backup_restore.py --backup
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))
import config  # noqa: E402

DB_PATH = os.path.join(config.DATA_DIR, "watchtower.db")
BACKUP_DIR = os.environ.get("TRAFFICGEN_BACKUP_DIR", os.path.join(config.DATA_DIR, "backups"))
REPORT_PATH = os.path.join(REPO_ROOT, "reports", "dr-trafficgen.json")
RETENTION = int(os.environ.get("TRAFFICGEN_BACKUP_KEEP", "48"))


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def table_digest(conn, table):
    """(число строк, sha256 содержимого). Порядок фиксирован — иначе сумма плавает."""
    cur = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
    cols = [d[0] for d in cur.description]
    h = hashlib.sha256()
    count = 0
    for row in cur:
        count += 1
        h.update(json.dumps(dict(zip(cols, row)), sort_keys=True, default=str).encode())
    return count, h.hexdigest()


def snapshot_digest(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {t: table_digest(conn, t) for t in table_names(conn)}
    finally:
        conn.close()


def last_event_ts(db_path):
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(timestamp) FROM events").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def do_backup(db_path=DB_PATH, out_dir=BACKUP_DIR):
    if not os.path.exists(db_path):
        return {"ok": False, "error": f"нет БД: {db_path}"}
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = os.path.join(out_dir, f"watchtower-{stamp}.db")
    started = time.monotonic()
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        # sqlite3.Connection.backup — онлайн-копия с согласованным снимком,
        # а не «скопировать файл», который может быть в середине транзакции.
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    seconds = round(time.monotonic() - started, 3)
    size = os.path.getsize(target)
    prune_old(out_dir)
    return {
        "ok": True,
        "file": target,
        "bytes": size,
        "seconds": seconds,
        "createdAt": now_utc_iso(),
        "sourceBytes": os.path.getsize(db_path),
    }


def prune_old(out_dir, keep=RETENTION):
    files = sorted(
        (f for f in os.listdir(out_dir) if f.startswith("watchtower-") and f.endswith(".db")),
        reverse=True)
    removed = []
    for f in files[keep:]:
        os.remove(os.path.join(out_dir, f))
        removed.append(f)
    return removed


def latest_backup(out_dir=BACKUP_DIR):
    if not os.path.isdir(out_dir):
        return None
    files = sorted((f for f in os.listdir(out_dir)
                    if f.startswith("watchtower-") and f.endswith(".db")), reverse=True)
    return os.path.join(out_dir, files[0]) if files else None


def do_verify(backup_path=None, db_path=DB_PATH):
    backup_path = backup_path or latest_backup()
    if not backup_path or not os.path.exists(backup_path):
        return {"ok": False, "error": "бэкап не найден — сначала --backup"}
    started = time.monotonic()
    tmp_dir = tempfile.mkdtemp(prefix="tc-restore-")
    restored = os.path.join(tmp_dir, "restored.db")
    try:
        shutil.copy2(backup_path, restored)
        live = snapshot_digest(db_path) if os.path.exists(db_path) else {}
        copy = snapshot_digest(restored)
        seconds = round(time.monotonic() - started, 3)

        mismatches = []
        for table, digest in copy.items():
            if table not in live:
                continue
            if live[table] != digest:
                mismatches.append({"table": table, "live": live[table], "restored": digest})
        # Восстановленная копия обязана открываться и читаться
        conn = sqlite3.connect(restored)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()

        rpo = None
        last_ts = last_event_ts(db_path)
        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                backup_dt = datetime.fromtimestamp(os.path.getmtime(backup_path), timezone.utc)
                rpo = round(max(0.0, (last_dt - backup_dt).total_seconds()), 1)
            except Exception:
                rpo = None

        return {
            "ok": integrity == "ok" and not mismatches,
            "backup": backup_path,
            "restoreSeconds": seconds,
            "integrityCheck": integrity,
            "restoredEvents": events,
            "tablesCompared": len(copy),
            "mismatches": mismatches,
            "rpoSeconds": rpo,
            "verifiedAt": now_utc_iso(),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="DR: бэкап и проверенное восстановление")
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--report", action="store_true", help="бэкап + проверка +reports/dr-trafficgen.json")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default=BACKUP_DIR)
    args = ap.parse_args(argv)

    if not (args.backup or args.verify or args.report):
        args.report = True

    report = {"generatedAt": now_utc_iso(), "db": args.db, "backupDir": args.out}
    if args.backup or args.report:
        report["backup"] = do_backup(args.db, args.out)
    if args.verify or args.report:
        report["verify"] = do_verify(None, args.db)
    report["ok"] = bool(report.get("backup", {}).get("ok", True)) and \
        bool(report.get("verify", {}).get("ok", True))

    if args.report:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        report["reportPath"] = os.path.relpath(REPORT_PATH, REPO_ROOT)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
