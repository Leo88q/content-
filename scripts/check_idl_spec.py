#!/usr/bin/env python3
"""
Сверяет backend/src/chain/moderate.spec.json с настоящим IDL программы.

Зачем: backend отдаёт кошельку описание инструкции, и это описание обязано
совпадать с программой. Раньше оно писалось руками в обработчике и разошлось
в трёх местах (Option-аргументы и неполный список аккаунтов). Расхождение не
видно ни компилятору, ни юнит-тестам — оно проявляется только при попытке
собрать транзакцию.

Скрипт печатает фактический вид инструкции из IDL всегда, а не только при
ошибке: так один прогон даёт эталон для правки.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "backend/src/chain/moderate.spec.json"


def fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def flatten_accounts(accounts):
    """IDL Anchor хранит аккаунты либо плоским списком, либо с вложенными pda-группами."""
    out = []
    for a in accounts or []:
        if "accounts" in a:  # вложенная группа
            out.extend(flatten_accounts(a["accounts"]))
        else:
            out.append(a)
    return out


def flags(acc):
    return (bool(acc.get("writable", acc.get("isMut", False))),
            bool(acc.get("signer", acc.get("isSigner", False))))


def norm_type(t):
    """Приводит тип к каноническому виду для сравнения."""
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        if "option" in t:
            return {"option": norm_type(t["option"])}
        if "vec" in t:
            return {"vec": norm_type(t["vec"])}
        if "defined" in t:
            return {"defined": t["defined"].get("name", t["defined"]) if isinstance(t["defined"], dict) else t["defined"]}
    return t


def main() -> int:
    if len(sys.argv) != 2:
        fail(f"использование: {sys.argv[0]} <путь к idl.json>")
    idl_path = Path(sys.argv[1])
    if not idl_path.exists():
        fail(f"IDL не найден: {idl_path}")
    if not SPEC_PATH.exists():
        fail(f"спека не найдена: {SPEC_PATH}")

    idl = json.loads(idl_path.read_text(encoding="utf-8"))
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    name = spec["instruction"]

    instructions = {i["name"]: i for i in idl.get("instructions", [])}
    print(f"Инструкций в IDL: {len(instructions)}")
    print(f"Имена: {', '.join(sorted(instructions))}")

    if name not in instructions:
        fail(f"в IDL нет инструкции {name!r}; есть: {sorted(instructions)}")
    ix = instructions[name]

    # Печатаем эталон всегда — это то, с чем надо сверять спеку.
    print(f"\n--- фактическая инструкция {name!r} из IDL ---")
    print("args:", json.dumps(ix.get("args", []), ensure_ascii=False))
    print("accounts:", json.dumps(flatten_accounts(ix.get("accounts", [])), ensure_ascii=False))
    print("--------------------------------------------\n")

    errors = []

    # --- аргументы: имена, порядок, типы
    idl_args = ix.get("args", [])
    spec_args = spec["args"]
    if len(idl_args) != len(spec_args):
        errors.append(f"аргументов: в спеке {len(spec_args)}, в IDL {len(idl_args)}")
    for i, (s, d) in enumerate(zip(spec_args, idl_args)):
        if s["name"] != d.get("name"):
            errors.append(f"аргумент #{i}: в спеке {s['name']!r}, в IDL {d.get('name')!r}")
        st, dt = norm_type(s["type"]), norm_type(d.get("type"))
        if st != dt:
            errors.append(f"аргумент {s['name']!r}: в спеке тип {st!r}, в IDL {dt!r}")

    # --- аккаунты: имена, порядок, флаги
    idl_accs = flatten_accounts(ix.get("accounts", []))
    spec_accs = spec["accounts"]
    if len(idl_accs) != len(spec_accs):
        errors.append(f"аккаунтов: в спеке {len(spec_accs)}, в IDL {len(idl_accs)}")
    for i, (s, d) in enumerate(zip(spec_accs, idl_accs)):
        if s["name"] != d.get("name"):
            errors.append(f"аккаунт #{i}: в спеке {s['name']!r}, в IDL {d.get('name')!r}")
            continue
        sf, df = (s["isMut"], s["isSigner"]), flags(d)
        if sf != df:
            errors.append(
                f"аккаунт {s['name']!r}: в спеке (writable={sf[0]}, signer={sf[1]}), "
                f"в IDL (writable={df[0]}, signer={df[1]})"
            )

    if errors:
        print("::error::Спецификация backend расходится с IDL программы:")
        for e in errors:
            print(f"::error:: - {e}")
        print("\nИсправьте backend/src/chain/moderate.spec.json по эталону выше.")
        return 1

    print(f"OK: спецификация {name!r} совпадает с IDL "
          f"({len(spec_args)} аргументов, {len(spec_accs)} аккаунтов)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
