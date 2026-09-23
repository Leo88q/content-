#!/usr/bin/env python3
"""Целевой ре-аудит ончейн-правил sixsec: SW009 / SW010 / SW013 / SW016 / SW024.

Зачем свой проверяльщик, а не «перезапустить исходный сканер»:
сканер, которым получен reports/trafficgen-audit.json (10 findings, 2026-09),
в открытом доступе не опубликован — в хабе Leo88q/Games-watchtower лежит
только его ВЫХОД (reports/trafficgen-audit.json), самой утилиты нет
(проверено: `ls scripts/` в хабе -> только smoke-test.mjs). Значит «повторный
аудит» в этой песочнице равен воспроизводимой проверке самих правил, а не
повторному прогону чужого бинарника. Правила ниже implementированы по тексту
findings из исходного отчёта (rule_id + severity + help), поэтому результат
сопоставим с ним построчно.

Что проверяется
---------------
SW009 (high)     — у мутируемого token-аккаунта нет `token::mint`:
                   атакующий подставляет аккаунт другого минта.
SW010 (critical) — у мутируемого token-аккаунта нет `token::authority`:
                   атакующий подставляет аккаунт, владельцем которого он сам является.
SW013 (medium)   — PDA объявлен через `seeds`, но без `bump`: сиды не проверяются.
SW016 (high)     — `init_if_needed` вне allowlist'а принятых рисков.
SW024 (high)     — сырое деление/остаток от деления вне checked_* (деление на ноль).

Принятые риски (acceptedRisks) не удаляются из отчёта, а выносятся в отдельный
раздел: находка остаётся видимой, но не блокирует гейт, пока у неё есть
владелец, обоснование и срок пересмотра.

Выход
-----
  reports/trafficgen-audit.json — машинночитаемый отчёт (схема совместима с
                                  форматом findings исходного аудита)
  stdout                        — таблица находок и вердикт

Коды возврата
-------------
  0 — нет открытых critical/high (accepted risks не считаются открытыми)
  1 — есть открытые critical/high (или medium при --strict)
  2 — не найден исходник / ошибка разбора

Использование
-------------
  python3 scripts/check_solana_constraints.py
  python3 scripts/check_solana_constraints.py --strict
  python3 scripts/check_solana_constraints.py --output reports/trafficgen-audit.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_TARGET = os.path.join("programs", "sixsec", "src", "lib.rs")
DEFAULT_OUTPUT = os.path.join("reports", "trafficgen-audit.json")

# ---------------------------------------------------------------------------
# Принятые риски. Каждый — с владельцем, обоснованием и датой пересмотра.
# Ключ: (rule_id, struct, field).
# ---------------------------------------------------------------------------
ACCEPTED_RISKS = [
    {
        "rule_id": "SW016",
        "struct": "CreateTask",
        "field": "mint_reserve",
        "owner": "SixSec protocol lead",
        "justification": (
            "PDA [RESERVE_SEED, reward_mint]; адрес выводится из минта, а не из "
            "пользовательского ключа; аккаунт не хранит полномочий (только "
            "{mint, reserved}); повторный вызов не обнуляет `reserved`, потому что "
            "инструкция пишет reserved += worst_case_reserve, а не инициализирует его. "
            "Замена на `init` сломала бы повторное создание задания под уже "
            "зарезервированный минт (ADR-0015)."
        ),
        "compensating_controls": [
            "constraint mint_reserve.mint == reward_mint.key() || Pubkey::default() -> ReserveMintMismatch",
            "seeds = [RESERVE_SEED, reward_mint.key()] + bump",
            "тест constraints_guard::init_if_needed_only_inside_accepted_allowlist падает при появлении нового init_if_needed",
        ],
        "review_by": "2026-12-31",
        "status": "accepted",
    },
]

ALLOWED_INIT_IF_NEEDED = {(r["struct"], r["field"]) for r in ACCEPTED_RISKS}

# ---------------------------------------------------------------------------
# Разбор исходника
# ---------------------------------------------------------------------------
FIELD_RE = re.compile(r"^pub\s+(?P<name>\w+)\s*:\s*(?P<type>.+?),?\s*$")
STRUCT_RE = re.compile(r"^pub\s+struct\s+(?P<name>\w+)")
ACCOUNT_ATTR_RE = re.compile(r"^#\[\s*account\s*\(")

# Операторы деления вне комментариев и строк.
DIV_RE = re.compile(r"(?<![/\w])([/%])(?![/*])")


class Field:
    __slots__ = ("name", "type", "attr", "line", "struct")

    def __init__(self, name, ftype, attr, line, struct):
        self.name = name
        self.type = ftype
        self.attr = attr
        self.line = line
        self.struct = struct

    @property
    def is_token_account(self) -> bool:
        return "TokenAccount" in self.type

    @property
    def is_mutable(self) -> bool:
        return bool(re.search(r"(^|[^a-z_])mut([^a-z_]|$)", self.attr))


def strip_line_comment(line: str) -> str:
    """Убирает `//`-комментарий, не трогая `//` внутри строк и URL в комментариях."""
    out = []
    in_string = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"' and (i == 0 or line[i - 1] != "\\"):
            in_string = not in_string
        if not in_string and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def parse_fields(lines):
    """Возвращает список Field: объявления полей с их #[account(...)] атрибутами."""
    fields = []
    pending_attr = None
    pending_line = None
    current_struct = None
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        m_struct = STRUCT_RE.match(stripped)
        if m_struct:
            current_struct = m_struct.group("name")

        if ACCOUNT_ATTR_RE.match(stripped):
            buf = []
            depth = 0
            start = i
            while i < n:
                code = strip_line_comment(lines[i])
                buf.append(code)
                depth += code.count("(") - code.count(")")
                i += 1
                if depth <= 0:
                    break
            pending_attr = "\n".join(buf)
            pending_line = start + 1
            continue

        if stripped.startswith("#[") or stripped.startswith("//") or stripped.startswith("///"):
            i += 1
            continue

        m = FIELD_RE.match(stripped)
        if m:
            if pending_attr is not None:
                fields.append(
                    Field(
                        name=m.group("name"),
                        ftype=m.group("type").strip(),
                        attr=pending_attr,
                        line=pending_line,
                        struct=current_struct,
                    )
                )
            pending_attr = None
            pending_line = None
        i += 1
    return fields


def finding(rule_id, severity, message, path, line, help_text):
    return {
        "rule_id": rule_id,
        "severity": severity,
        "message": message,
        "location": {"path": path, "line": line, "column": 1},
        "help": help_text,
        "suppressed": False,
    }


def run_checks(source: str, rel_path: str):
    lines = source.splitlines()
    fields = parse_fields(lines)
    findings = []

    # --- SW009 / SW010: token-аккаунты ---
    for f in fields:
        if not f.is_token_account:
            continue
        has_mint = bool(re.search(r"(token|associated_token)::mint\s*=", f.attr))
        has_authority = bool(re.search(r"(token|associated_token)::authority\s*=", f.attr))
        if not has_mint:
            findings.append(
                finding(
                    "SW009",
                    "high",
                    f"Mutable token account `{f.name}` has no `token::mint` constraint; "
                    "an attacker can substitute a token account for a different mint",
                    rel_path,
                    f.line,
                    "Add #[account(mut, token::mint = <mint_field>)] to pin this account "
                    "to the expected mint, or use associated_token::mint = <mint_field>.",
                )
            )
        if not has_authority:
            findings.append(
                finding(
                    "SW010",
                    "critical",
                    f"Mutable token account `{f.name}` has no `token::authority` constraint; "
                    "an attacker can pass a token account they own as the signer's account",
                    rel_path,
                    f.line,
                    "Add token::authority = <signer_field> to pin this account to the "
                    "expected owner, or use associated_token::authority = <signer_field>.",
                )
            )

    # --- SW013: PDA с seeds, но без bump ---
    for f in fields:
        if f.is_token_account:
            continue
        if "seeds" in f.attr and "bump" not in f.attr:
            findings.append(
                finding(
                    "SW013",
                    "medium",
                    f"Account `{f.name}` declares `seeds` without `bump`: PDA не "
                    "проверяется на принадлежность канонической точке",
                    rel_path,
                    f.line,
                    "Добавьте `bump` (или `bump = <field>`) в #[account(...)].",
                )
            )

    # --- SW016: init_if_needed ---
    fields_by_name = {(f.struct, f.name): f for f in fields}
    for idx, line in enumerate(lines):
        if "init_if_needed" not in strip_line_comment(line):
            continue
        struct, fname = _enclosing_field(lines, idx, fields)
        key = (struct, fname)
        if key in ALLOWED_INIT_IF_NEEDED:
            # Находка остаётся в отчёте, но помеченной принятым риском:
            # исчезновение строки из findings без следа было бы скрытием.
            risk = next(r for r in ACCEPTED_RISKS if (r["struct"], r["field"]) == key)
            findings.append({**finding(
                "SW016",
                "high",
                f"Account `{fname}` uses `init_if_needed`; review for "
                "re-initialization or state-reset risk.",
                rel_path,
                idx + 1,
                "Prefer #[account(init, ...)] when possible.",
            ), "acceptedRisk": risk, "struct": struct})
            continue
        findings.append(
            finding(
                "SW016",
                "high",
                f"Account `{fname or '?'}` uses `init_if_needed`; review for "
                "re-initialization or state-reset risk.",
                rel_path,
                idx + 1,
                "Prefer #[account(init, ...)] when possible. If init_if_needed is "
                "necessary, confirm the account cannot be abused to reset state.",
            )
        )

    # --- SW024: сырое деление ---
    for idx, raw in enumerate(lines):
        code = strip_line_comment(raw)
        code = re.sub(r'"[^"]*"', '""', code)
        for m in DIV_RE.finditer(code):
            op = m.group(1)
            if op == "/" and re.search(r"\b(https?:|///|\* )", code[: m.start()]):
                continue
            window = "\n".join(
                strip_line_comment(x) for x in lines[max(0, idx - 3) : idx + 2]
            )
            if re.search(r"checked_(div|rem)|saturating_div|nonzero|!= 0|require!", window):
                continue
            findings.append(
                finding(
                    "SW024",
                    "high",
                    f"Возможное деление на ноль: сырой оператор `{op}` без проверки "
                    "делителя или checked_* обёртки.",
                    rel_path,
                    idx + 1,
                    "Используйте checked_div/checked_rem или require!(делитель != 0).",
                )
            )
    return findings


def _enclosing_field(lines, idx, fields):
    """Ищет ближайшее объявление поля ниже строки idx (атрибут precedes поле)."""
    for j in range(idx, min(idx + 12, len(lines))):
        m = FIELD_RE.match(lines[j].strip())
        if m:
            struct = None
            for k in range(j, -1, -1):
                ms = STRUCT_RE.match(lines[k].strip())
                if ms:
                    struct = ms.group("name")
                    break
            return struct, m.group("name")
    return None, None


def build_report(findings, target, source_sha):
    open_findings = []
    accepted = []
    for f in findings:
        risk = f.get("acceptedRisk") or _match_risk(f)
        if risk:
            entry = {k: v for k, v in f.items() if k not in ("acceptedRisk", "struct")}
            accepted.append({**entry, "acceptedRisk": risk})
        else:
            open_findings.append({k: v for k, v in f.items() if k != "struct"})

    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in open_findings:
        sev = f["severity"]
        summary[sev] = summary.get(sev, 0) + 1

    return {
        "schema": "trafficgen-audit/v2",
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": {
            "name": "scripts/check_solana_constraints.py",
            "kind": "targeted-rule-recheck",
            "note": (
                "Исходный сканер, создавший аудит 2026-09, не опубликован; здесь "
                "воспроизведены его правила по тексту findings. Строки могут "
                "отличаться от исходного отчёта — код правился."
            ),
        },
        "target": {"path": target, "sourceSha256Prefix": source_sha[:16]},
        "summary": {
            "open": summary,
            "accepted": len(accepted),
            "total": len(findings),
        },
        "findings": open_findings,
        "acceptedRisks": accepted,
        "gate": {
            "policy": "блокирует CI при открытых critical/high; medium блокирует только в --strict",
            "passed": summary["critical"] == 0 and summary["high"] == 0,
        },
    }


def _match_risk(f):
    struct, field = _field_from_message(f["message"])
    for risk in ACCEPTED_RISKS:
        if risk["rule_id"] == f["rule_id"] and risk["field"] == field and risk["struct"] == struct:
            return risk
    return None


def _field_from_message(message):
    m = re.search(r"Account `(\w+)`|account `(\w+)`", message)
    field = m.group(1) or m.group(2) if m else None
    return None, field


# ---------------------------------------------------------------------------
# Самопроверка правил: правила, которые никогда не срабатывают, ничем не лучше
# отсутствующих. Каждый кейс — минимальный исходник, на котором правило ОБЯЗАНО
# найти нарушение; иначе --self-test падает.
# ---------------------------------------------------------------------------
SELF_TEST_CASES = [
    {
        "name": "SW009+SW010: мутируемый token-аккаунт без mint и authority",
        "source": """
#[derive(Accounts)]
pub struct Payout<'info> {
    #[account(mut)]
    pub prize_pool: InterfaceAccount<'info, TokenAccount>,
    pub reward_mint: InterfaceAccount<'info, Mint>,
}
""",
        "expect": {"SW009", "SW010"},
    },
    {
        "name": "SW010: mint пришпилён, authority — нет",
        "source": """
#[derive(Accounts)]
pub struct Payout<'info> {
    #[account(mut, token::mint = reward_mint)]
    pub prize_pool: InterfaceAccount<'info, TokenAccount>,
    pub reward_mint: InterfaceAccount<'info, Mint>,
}
""",
        "expect": {"SW010"},
    },
    {
        "name": "SW016: init_if_needed вне allowlist'а",
        "source": """
#[derive(Accounts)]
pub struct CreateTask<'info> {
    #[account(init_if_needed, payer = creator, space = 8)]
    pub task: Account<'info, TaskAccount>,
}
""",
        "expect": {"SW016"},
    },
    {
        "name": "SW013: PDA с seeds, но без bump",
        "source": """
#[derive(Accounts)]
pub struct Withdraw<'info> {
    #[account(mut, seeds = [b"pool_state"])]
    pub pool_state: Account<'info, PoolState>,
}
""",
        "expect": {"SW013"},
    },
    {
        "name": "SW024: сырое деление без проверки делителя",
        "source": """
pub fn share(total: u64, parts: u64) -> u64 {
    total / parts
}
""",
        "expect": {"SW024"},
    },
    {
        "name": "чистый исходник: канонические констрейнты на месте",
        "source": """
#[derive(Accounts)]
pub struct Payout<'info> {
    #[account(mut, seeds = [b"prize", reward_mint.key().as_ref()], bump,
              token::mint = reward_mint, token::authority = prize_pool)]
    pub prize_pool: InterfaceAccount<'info, TokenAccount>,
    pub reward_mint: InterfaceAccount<'info, Mint>,
}
""",
        "expect": set(),
    },
]


def self_test():
    ok = True
    for case in SELF_TEST_CASES:
        found = {f["rule_id"] for f in run_checks(case["source"], "self-test.rs")}
        expected = set(case["expect"])
        status = "ok " if found == expected else "FAIL"
        if found != expected:
            ok = False
        print(f"  [{status}] {case['name']}: ожидались {sorted(expected) or '—'}, "
              f"найдены {sorted(found) or '—'}")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ре-аудит ончейн-правил sixsec")
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--strict", action="store_true", help="блокировать и на medium")
    parser.add_argument("--self-test", action="store_true",
                        help="проверить, что правила вообще способны сработать")
    args = parser.parse_args(argv)

    if args.self_test:
        print("self-test правил:")
        return self_test()

    target_path = args.target if os.path.isabs(args.target) else os.path.join(ROOT, args.target)
    if not os.path.exists(target_path):
        print(f"нет исходника: {target_path}", file=sys.stderr)
        return 2
    with open(target_path, "r", encoding="utf-8") as fh:
        source = fh.read()

    import hashlib

    sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    rel_path = os.path.relpath(target_path, ROOT).replace(os.sep, "/")
    findings = run_checks(source, rel_path)
    report = build_report(findings, rel_path, sha)

    out_path = args.output if os.path.isabs(args.output) else os.path.join(ROOT, args.output)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    s = report["summary"]["open"]
    print(f"target: {rel_path}")
    print(f"sha256: {sha[:16]}… (в отчёте только префикс: полный 64-hex ловится сканером секретов)")
    print(
        "открытых: critical={critical} high={high} medium={medium}; принятых рисков: {a}".format(
            a=report["summary"]["accepted"], **s
        )
    )
    for f in report["findings"]:
        print(f"  [{f['severity']:8}] {f['rule_id']} {rel_path}:{f['location']['line']} — {f['message']}")
    for f in report["acceptedRisks"]:
        risk = f["acceptedRisk"]
        print(
            f"  [accepted ] {f['rule_id']} {rel_path}:{f['location']['line']} — {risk['field']} "
            f"(владелец: {risk['owner']}, пересмотр до {risk['review_by']})"
        )
    print(f"отчёт: {os.path.relpath(out_path, ROOT)}")

    if s["critical"] or s["high"]:
        return 1
    if args.strict and s["medium"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
