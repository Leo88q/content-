#!/usr/bin/env python3
"""Точка входа конвейера фабрики: `npm run build` → `python3 site/factory/generate_all.py`.

Раньше npm-скрипт `build` указывал на этот файл, которого не существовало: локальная сборка
падала с `[Errno 2] No such file or directory`, а CI обходил проблему, вызывая шаги по
одиночке (.github/workflows/factory.yml). Теперь порядок шагов живёт здесь и совпадает с CI.

Шаги                Что делает                                  Если не может
  fetch_data          свежий снимок рынка (GeckoTerminal)        сеть недоступна → предупреждение
  build_seo           программные SEO-страницы по реестру        обязателен
  make_cards          PNG-карточки 1200x630                      нет Pillow → предупреждение
  make_videos         вертикальные mp4 (ffmpeg)                  нет ffmpeg → предупреждение
  make_vs             ончейн-баттлы X vs Y                       обязателен
  make_digest         дневной дайджест (md + txt для постов)     обязателен
  post_x              публикация в X                             только с --post-x (или X_POST=1)
  prune               чистка старых артефактов                   предупреждение

Флаги:
  --dry-run        показать план, ничего не запускать
  --check          проверить, что скрипты шагов на месте (быстрая защита от «файла нет»)
  --only=a,b       выполнить только эти шаги
  --skip=a,b       пропустить шаги
  --post-x         включить публикацию в X (по умолчанию выключена: side-effect наружу)
  --keep-going     не останавливаться на первом падении
  --strict         мягкие шаги тоже считают падением (для CI)
  --json         машиночитаемый итог
  --timeout=SEC    лимит на шаг (по умолчанию 900)

Код возврата: 0 — все обязательные шаги зелёные; 1 — есть падение.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(SITE_DIR)

# (id, относительно репозитория, обязательный, заметка о «мягкости»)
STEPS = [
    ("fetch_data", "site/factory/fetch_data.py", False, "нужна сеть; без неё работает по прошлому снимку"),
    ("build_seo", "site/build_seo.py", True, "SEO-страницы — основа сайта"),
    ("make_cards", "site/factory/make_cards.py", False, "нужен Pillow"),
    ("make_videos", "site/factory/make_videos.py", False, "нужен ffmpeg"),
    ("make_vs", "site/factory/make_vs.py", True, "баттлы X vs Y"),
    ("make_digest", "site/factory/make_digest.py", True, "дайджест для постов"),
    ("post_x", "site/factory/post_x.py", False, "внешний side-effect: только с --post-x"),
    ("prune", "site/factory/prune.py", False, "чистка старых артефактов"),
]


def _select(only, skip):
    chosen = []
    for step_id, script, required, note in STEPS:
        if only and step_id not in only:
            continue
        if step_id in skip:
            continue
        chosen.append((step_id, script, required, note))
    return chosen


def _plan(post_x_enabled, only=(), skip=()):
    """Список шагов к исполнению. post_x попадает сюда ТОЛЬКО когда его явно включили."""
    steps = []
    for step_id, script, required, note in STEPS:
        if only and step_id not in only:
            continue
        if step_id in skip:
            continue
        if step_id == "post_x" and not post_x_enabled:
            continue
        steps.append({
            "id": step_id, "script": script, "required": required, "note": note,
            "exists": os.path.exists(os.path.join(REPO_ROOT, script)),
        })
    return steps


def _split_csv(value):
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    flag = lambda name: f"--{name}" in argv
    opt = lambda name, dflt="": next((a.split("=", 1)[1] for a in argv if a.startswith(f"--{name}=")), dflt)

    only, skip = _split_csv(opt("only")), _split_csv(opt("skip"))
    post_x = flag("post-x") or os.environ.get("X_POST") == "1"
    timeout = int(opt("timeout", "900") or 900)

    plan = _plan(post_x, only, skip)

    if flag("check"):
        bad = [s for s in plan if s["required"] and not s["exists"]]
        for s in plan:
            mark = "✓" if s["exists"] else ("✗" if s["required"] else "!")
            print(f"  {mark} {s['id']:<12} {s['script']}{'' if s['exists'] else ' — нет файла'}")
        if bad:
            print(f"\n✗ обязательные шаги отсутствуют: {', '.join(s['id'] for s in bad)}", file=sys.stderr)
            return 1
        print(f"\n✓ план валиден: {len(plan)} шаг(ов)")
        return 0

    if flag("dry-run"):
        print("План сборки (ничего не запускаем):")
        for s in plan:
            how = "обязательный" if s["required"] else "мягкий"
            gated = "  [внешний side-effect]" if s["id"] == "post_x" else ""
            print(f"  · {s['id']:<12} python {s['script']}  ({how}){gated}")
        if "post_x" not in [s["id"] for s in plan]:
            print("  · post_x       пропущен: публикация в X выключена (--post-x или X_POST=1)")
        return 0

    results, failed = [], False
    for s in plan:
        if not s["exists"]:
            line = f"  ✗ {s['id']:<12} нет файла {s['script']}"
            print(line)
            results.append({"id": s["id"], "status": "missing"})
            if s["required"]:
                failed = True
                if not flag("keep-going"):
                    break
            continue
        started = time.monotonic()
        print(f"▶ {s['id']}  ({s['script']})")
        try:
            proc = subprocess.run([sys.executable, os.path.join(REPO_ROOT, s["script"])],
                                  cwd=REPO_ROOT, timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code, err = 124, f"таймаут {timeout}с"
        except OSError as err:
            code, err = 127, str(err)
        else:
            err = ""
        secs = time.monotonic() - started
        if code == 0:
            print(f"✓ {s['id']} за {secs:.1f}с")
            results.append({"id": s["id"], "status": "ok", "seconds": round(secs, 1)})
        else:
            kind = "обязательный шаг упал" if s["required"] else f"мягкий шаг пропущен ({s['note']})"
            print(f"! {s['id']}: код {code} — {kind}{'; ' + err if err else ''}")
            results.append({"id": s["id"], "status": "fail", "code": code, "required": s["required"]})
            if s["required"] or flag("strict"):
                failed = True
                if not flag("keep-going"):
                    break

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\nИтог: {ok} ok, {len(results) - ok} проблемных из {len(plan)} шагов(а)")
    if flag("json"):
        print(json.dumps({"results": results, "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
