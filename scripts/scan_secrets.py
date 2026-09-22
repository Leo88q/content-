#!/usr/bin/env python3
"""Secret-сканер репозитория (PROMPT_TRAFFIC_GENERATOR_INTEGRATION.md §10.1).

Проверяет все git-tracked файлы на типовые форматы секретов. Без внешних зависимостей
(gitleaks/trufflehog в песочнице/CI могут отсутствовать — этот сканер всегда доступен).

Allowlist:
- сам скрипт (содержит паттерны)
- .env.example (только пустые плейсхолдеры)
- бинарные/медиа-артефакты (шрифты, видео), .git
- явные документальные упоминания (строки с 'пример', 'example', 'placeholder' рядом)

Exit code: 0 = чисто, 1 = найдены кандидаты.
"""

import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PATTERNS = [
    ("PEM private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub PAT (ghp_)", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("OpenAI/Stripe-style sk-", re.compile(r"\bsk-(?:proj-|live-)?[A-Za-z0-9_\-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("JWT (header.payload.signature)", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
    ("Solana/SSH hex (см. allowlist документов)", re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")),
    ("Generic password assignment", re.compile(
        r"(?i)\b(password|passwd|secret[_-]?key|api[_-]?secret)\b\s*[=:]\s*[\"'][^\"'\s]{8,}[\"']")),
    ("Keypair JSON (64+ ints array)", re.compile(r"\[\s*\d{1,3}\s*(?:,\s*\d{1,3}\s*){60,}\]")),
]

ALLOWLIST_FILES = {
    "scripts/scan_secrets.py",
    ".env.example",
}
ALLOWLIST_PREFIXES = (
    ".git/", "site/factory/fonts/", "site/videos/", "backend/src/api/fonts/",
    "target/",
)
BINARY_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov",
                     ".webm", ".woff2", ".woff", ".ttf", ".otf", ".ico", ".pdf",
                     ".zip", ".gz", ".lock", ".svgz")
# строки, где секрет встречается только в документации/примерах
DOC_MARKERS = re.compile(
    r"(?i)(example|placeholder|sample|dummy|пример|заменить|your[-_ ]|<[A-Z_]+>|xxxx|\*{4,})")


def tracked_files():
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=REPO_ROOT)
    except Exception:
        return []
    return out.decode().splitlines()


def is_allowed(rel):
    if rel in ALLOWLIST_FILES:
        return True
    if rel.startswith(ALLOWLIST_PREFIXES):
        return True
    if rel.lower().endswith(BINARY_EXTENSIONS):
        return True
    return False


def scan():
    findings = []
    files = tracked_files()
    if not files:
        print("!! git ls-files пуст — сканирую файловую систему напрямую")
        for root, dirs, names in os.walk(REPO_ROOT):
            if ".git" in root:
                continue
            for n in names:
                files.append(os.path.relpath(os.path.join(root, n), REPO_ROOT))
    scanned = 0
    for rel in files:
        if is_allowed(rel):
            continue
        path = os.path.join(REPO_ROOT, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception:
            continue
        if b"\x00" in raw[:4096]:
            continue  # бинарный
        data = raw.decode("utf-8", "replace")
        scanned += 1
        for name, pat in PATTERNS:
            for m in pat.finditer(data):
                line_start = data.rfind("\n", 0, m.start()) + 1
                line_end = data.find("\n", m.end())
                line = data[line_start: line_end if line_end != -1 else len(data)]
                if DOC_MARKERS.search(line):
                    continue
                col = m.start() - line_start
                findings.append((rel, name, line.strip()[:160], col))
    return files, scanned, findings


def main():
    files, scanned, findings = scan()
    print(f"[scan_secrets] файлов в git: {len(files)}, просканировано текстовых: {scanned}")
    if not findings:
        print("[scan_secrets] OK: кандидатов на секреты не найдено.")
        return 0
    print(f"[scan_secrets] НАЙДЕНО кандидатов: {len(findings)}")
    for rel, name, line, col in findings[:50]:
        print(f"  - {rel}: [{name}] ~col {col}: {line[:120]}")
    if len(findings) > 50:
        print(f"  ... и ещё {len(findings) - 50}")
    print("Действия: 1) ротация значения, 2) удаление из дерева, 3) план очистки истории "
          "— согласуется с человеком (PROMPT §10.1).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
