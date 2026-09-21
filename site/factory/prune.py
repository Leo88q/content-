#!/usr/bin/env python3
"""Шаг 5 фабрики: чистка старых артефактов (репозиторий не должен пухнуть).

- site/cards/<YYYY-MM-DD>/  старше CARDS_KEEP_DAYS  -> удалить (latest/ не трогаем)
- site/digests/<YYYY-MM-DD>.* старше DIGESTS_KEEP_DAYS -> удалить
"""
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def prune_dir(root, keep_days, is_dir):
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = 0
    if not os.path.isdir(root):
        return removed
    for name in os.listdir(root):
        m = DATE_RE.match(name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if d < cutoff:
            path = os.path.join(root, name)
            if is_dir and os.path.isdir(path):
                shutil.rmtree(path)
                removed += 1
            elif not is_dir and os.path.isfile(path):
                os.remove(path)
                removed += 1
    return removed


def main():
    n1 = prune_dir(config.CARDS_DIR, config.CARDS_KEEP_DAYS, is_dir=True)
    n2 = prune_dir(config.DIGESTS_DIR, config.DIGESTS_KEEP_DAYS, is_dir=False)
    n3 = prune_dir(config.VIDEOS_DIR, config.VIDEOS_KEEP_DAYS, is_dir=True)
    print(f"OK: prune — карточек-каталогов: {n1}, дайджестов: {n2}, видео-каталогов: {n3}")


if __name__ == "__main__":
    main()
