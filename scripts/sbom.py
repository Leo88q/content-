#!/usr/bin/env python3
"""SBOM (o-06): машиночитаемый перечень того, из чего собран trafficgen.

Смысл не в формальности, а в двух вопросах, на которые нужно уметь ответить
быстро: «что именно у нас в проде?» и «что надо чинить при следующем CVE?».
Поэтому перечисляем не только пакеты, но и пины тулчейна из CI: они влияют на
сборку программы не меньше зависимостей.

Источники
---------
- `.github/workflows/*.yml` — пины Anchor/Rust/Solana/Node/Python;
- `Cargo.lock` — версии Rust-зависимостей программы;
- `backend/package.json` — зависимости модерационного бэкенда;
- импорты `site/factory/*.py` — сторонние модули Python (Pillow, requests…).

Использование: `python3 scripts/sbom.py`
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO_ROOT, "reports", "sbom.json")

STDLIB_HINT = {
    "json", "os", "sys", "re", "math", "time", "uuid", "hmac", "hashlib", "base64",
    "sqlite3", "threading", "urllib", "statistics", "argparse", "tempfile", "shutil",
    "datetime", "typing", "unittest", "socket", "http", "subprocess", "pathlib",
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read(path):
    full = os.path.join(REPO_ROOT, path)
    if not os.path.exists(full):
        return None
    with open(full, "r", encoding="utf-8") as fh:
        return fh.read()


def ci_pins():
    pins = {}
    for name in ("ci.yml", "factory.yml"):
        text = read(os.path.join(".github", "workflows", name)) or ""
        for key in ("ANCHOR_VERSION", "RUST_VERSION", "SOLANA_VERSION"):
            m = re.search(rf'{key}:\s*"([^"]+)"', text)
            if m and key not in pins:
                pins[key] = m.group(1)
        for m in re.finditer(r"python-version:\s*\"([^\"]+)\"", text):
            pins.setdefault("PYTHON_VERSION", m.group(1))
        for m in re.finditer(r"node-version:\s*\"([^\"]+)\"", text):
            pins.setdefault("NODE_VERSION", m.group(1))
    return pins


def rust_packages():
    lock = read("Cargo.lock")
    if not lock:
        return []
    packages = []
    for block in lock.split("[[package]]")[1:]:
        name = re.search(r'name = "([^"]+)"', block)
        version = re.search(r'version = "([^"]+)"', block)
        if name and version:
            packages.append({"name": name.group(1), "version": version.group(1)})
    return packages


def node_dependencies():
    text = read(os.path.join("backend", "package.json"))
    if not text:
        return {}
    pkg = json.loads(text)
    return {
        "dependencies": pkg.get("dependencies", {}),
        "devDependencies": pkg.get("devDependencies", {}),
    }


def python_imports():
    """Сторонние модули, которые реально импортирует фабрика."""
    factory_dir = os.path.join(REPO_ROOT, "site", "factory")
    # Модули самой фабрики — не сторонние зависимости.
    local_modules = {f[:-3] for f in os.listdir(factory_dir) if f.endswith(".py")}
    found = {}
    for fname in sorted(os.listdir(factory_dir)):
        if not fname.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(factory_dir, fname), encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    found.setdefault(a.name.split(".")[0], set()).add(fname)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.setdefault(node.module.split(".")[0], set()).add(fname)
    third_party = {}
    for mod, files in sorted(found.items()):
        if mod in STDLIB_HINT or mod in sys.stdlib_module_names or mod in local_modules:
            continue
        version = None
        try:
            from importlib import metadata

            version = metadata.version(mod)
        except Exception:
            version = None
        third_party[mod] = {"version": version, "usedIn": sorted(files)}
    return {
        "thirdParty": third_party,
        "stdlibOnly": sorted(m for m in found
                             if m in STDLIB_HINT or m in sys.stdlib_module_names),
    }


def main():
    pins = ci_pins()
    rust = rust_packages()
    local = [p for p in rust if p["name"] in ("sixsec",)]
    sbom = {
        "schema": "trafficgen-sbom/v1",
        "generatedAt": now_utc_iso(),
        "component": "trafficgen (TalkChart Traffic Generator & Audience Layer)",
        "toolchainPins": pins,
        "rust": {
            "localPackages": local,
            "dependencyCount": len(rust) - len(local),
            "keyDependencies": [p for p in rust
                                if p["name"] in ("anchor-lang", "anchor-spl", "litesvm")],
        },
        "node": node_dependencies(),
        "python": python_imports(),
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(sbom, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: (v if k != "python" else {"thirdParty": v["thirdParty"]})
                      for k, v in sbom.items() if k != "rust"},
                     ensure_ascii=False, indent=2)[:1800])
    print(f"\nRust-пакетов в Cargo.lock: {len(rust)}")
    print(f"SBOM: {os.path.relpath(OUT_PATH, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
