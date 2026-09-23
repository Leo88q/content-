//! Охранник констрейнтов: проверяет invariants по ИСХОДНИКУ lib.rs, а не по
//! скомпилированной программе.
//!
//! Зачем так, а не только интеграционными тестами на LiteSVM: интеграционные
//! тесты SixSec запаркованы `#[ignore]` (ADR-0014 — litesvm 0.10 не грузит ELF
//! от тулчейна Anchor 1.2.0), то есть сегодня они ничего не доказывают. Этот
//! файл выполняется в обычном `cargo test` и ловит ровно тот класс ошибки,
//! который дал 3 critical в аудите: «добавили token-аккаунт и забыли
//! `token::mint` / `token::authority`».
//!
//! Границы честности: это проверка исходного текста, а не семантики Anchor.
//! Она гарантирует, что констрейнт НАПИСАН, но не то, что Anchor сгенерировал
//! корректную проверку. Семантику подтверждают интеграционные тесты, когда
//! они будут разблокированы (Surfpool).
//!
//! Тот же набор правил воспроизведён для CI на Python:
//! `scripts/check_solana_constraints.py` (там же — машинный отчёт
//! reports/trafficgen-audit.json и самопроверка правил `--self-test`).

use std::collections::HashSet;

const SRC: &str = include_str!("../src/lib.rs");

#[derive(Debug, Clone)]
struct FieldDecl {
    struct_name: String,
    field: String,
    /// Полная строка объявления — тип аккаунта живёт здесь, а не в атрибуте.
    decl: String,
    attr: String,
    line: usize,
}

/// Срезает `//`-комментарий. URL внутри комментариев (`https://`) не встречается
/// в проверяемых атрибутах, поэтому обработка строковых литералов не нужна.
fn strip_comment(line: &str) -> String {
    match line.find("//") {
        Some(idx) => line[..idx].to_string(),
        None => line.to_string(),
    }
}

fn struct_name_of(line: &str) -> Option<String> {
    let trimmed = line.trim();
    let rest = trimmed.strip_prefix("pub struct ")?;
    let name: String = rest
        .chars()
        .take_while(|c| c.is_alphanumeric() || *c == '_')
        .collect();
    if name.is_empty() {
        None
    } else {
        Some(name)
    }
}

/// `pub worker_ata: InterfaceAccount<'info, TokenAccount>,` -> Some("worker_ata")
fn field_name_of(line: &str) -> Option<String> {
    let trimmed = line.trim();
    if !trimmed.starts_with("pub ") {
        return None;
    }
    if !(trimmed.ends_with(',') || trimmed.ends_with(';') || trimmed.ends_with('>')) {
        return None;
    }
    let body = trimmed.trim_start_matches("pub ");
    let colon = body.find(':')?;
    let name = body[..colon].trim();
    if name.is_empty() || !name.chars().all(|c| c.is_alphanumeric() || c == '_') {
        return None;
    }
    Some(name.to_string())
}

fn parse_fields() -> Vec<FieldDecl> {
    let lines: Vec<&str> = SRC.lines().collect();
    let mut out = Vec::new();
    let mut current_struct = String::new();
    let mut pending: Option<(String, usize)> = None;
    let mut i = 0usize;

    while i < lines.len() {
        let trimmed = lines[i].trim();
        if let Some(name) = struct_name_of(lines[i]) {
            current_struct = name;
        }
        if trimmed.starts_with("#[account(") {
            let mut buf = String::new();
            let mut depth = 0i32;
            let start = i;
            while i < lines.len() {
                let code = strip_comment(lines[i]);
                for ch in code.chars() {
                    if ch == '(' {
                        depth += 1;
                    } else if ch == ')' {
                        depth -= 1;
                    }
                }
                buf.push_str(&code);
                buf.push('\n');
                i += 1;
                if depth <= 0 {
                    break;
                }
            }
            pending = Some((buf, start + 1));
            continue;
        }
        if trimmed.starts_with("#[") || trimmed.starts_with("//") {
            i += 1;
            continue;
        }
        if let Some(name) = field_name_of(lines[i]) {
            // Поля без `#[account(...)]` (минты, программы) тоже нужны: на них
            // ссылаются констрейнты `token::mint = reward_mint`.
            let (attr, line) = pending.take().unwrap_or_else(|| (String::new(), i + 1));
            out.push(FieldDecl {
                struct_name: current_struct.clone(),
                field: name,
                decl: trimmed.to_string(),
                attr,
                line,
            });
        }
        i += 1;
    }
    out
}

fn token_accounts() -> Vec<FieldDecl> {
    parse_fields()
        .into_iter()
        .filter(|f| f.decl.contains("TokenAccount"))
        .collect()
}

fn has_token_constraint(attr: &str, kind: &str) -> bool {
    attr.contains(&format!("token::{}", kind))
        || attr.contains(&format!("associated_token::{}", kind))
}

/// Значение констрейнта: `token::authority = prize_pool` -> Some("prize_pool").
fn constraint_target(attr: &str, kind: &str) -> Option<String> {
    for prefix in ["token::", "associated_token::"] {
        let needle = format!("{}{} =", prefix, kind);
        if let Some(idx) = attr.find(&needle) {
            let rest = &attr[idx + needle.len()..];
            let value: String = rest
                .trim_start()
                .chars()
                .take_while(|c| c.is_alphanumeric() || *c == '_')
                .collect();
            if !value.is_empty() {
                return Some(value);
            }
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Тест парсера: правила, которые ничего не находят, хуже отсутствующих правил.
// ---------------------------------------------------------------------------
#[test]
fn parser_finds_real_accounts_and_token_accounts() {
    let fields = parse_fields();
    assert!(
        fields.len() >= 15,
        "парсер должен видеть поля Accounts-структур, увидел {}",
        fields.len()
    );
    let tokens = token_accounts();
    assert!(
        tokens.len() >= 5,
        "в sixsec не меньше пяти token-аккаунтов (prize_pool, skr_pool, worker ATA, destination), увидел {}",
        tokens.len()
    );
    let names: HashSet<&str> = tokens.iter().map(|f| f.field.as_str()).collect();
    for expected in ["prize_pool", "worker_ata", "skr_pool", "worker_skr_ata", "destination"] {
        assert!(names.contains(expected), "не найден token-аккаунт {}", expected);
    }
}

// ---------------------------------------------------------------------------
// SW009 / SW010
// ---------------------------------------------------------------------------
#[test]
fn every_token_account_pins_its_mint() {
    for f in token_accounts() {
        assert!(
            has_token_constraint(&f.attr, "mint"),
            "SW009: у token-аккаунта `{}::{}` (строка {}) нет `token::mint`; \
             подмена аккаунта другого минта возможна",
            f.struct_name,
            f.field,
            f.line
        );
    }
}

#[test]
fn every_token_account_pins_its_authority() {
    for f in token_accounts() {
        assert!(
            has_token_constraint(&f.attr, "authority"),
            "SW010: у token-аккаунта `{}::{}` (строка {}) нет `token::authority`; \
             атакующий подставит аккаунт, которым владеет сам",
            f.struct_name,
            f.field,
            f.line
        );
    }
}

#[test]
fn token_authority_target_is_an_account_of_the_same_struct() {
    // Anchor раскрывает `token::authority = X` в `X.key()`. Pubkey-поле
    // (например `submission.worker`) не скомпилируется, поэтому цель обязана
    // быть аккаунтом этой же структуры — это и проверяем.
    let fields = parse_fields();
    for f in token_accounts() {
        let target = match constraint_target(&f.attr, "authority") {
            Some(t) => t,
            None => panic!("SW010: у {}::{} нет token::authority", f.struct_name, f.field),
        };
        assert!(
            fields.iter().any(|other| {
                other.struct_name == f.struct_name && other.field == target
            }),
            "SW010: `token::authority = {}` в {}::{} ссылается на то, чего нет \
             в этой структуре — Anchor не сгенерирует валидную проверку",
            target,
            f.struct_name,
            f.field
        );
    }
}

#[test]
fn token_mint_target_is_an_account_of_the_same_struct() {
    let fields = parse_fields();
    for f in token_accounts() {
        let target = match constraint_target(&f.attr, "mint") {
            Some(t) => t,
            None => panic!("SW009: у {}::{} нет token::mint", f.struct_name, f.field),
        };
        assert!(
            fields
                .iter()
                .any(|o| o.struct_name == f.struct_name && o.field == target),
            "SW009: `token::mint = {}` в {}::{} ссылается на несуществующий аккаунт",
            target,
            f.struct_name,
            f.field
        );
    }
}

// ---------------------------------------------------------------------------
// SW013: PDA без bump не проверяет каноническую точку
// ---------------------------------------------------------------------------
#[test]
fn pda_accounts_always_check_bump() {
    for f in parse_fields() {
        if f.attr.contains("seeds") {
            assert!(
                f.attr.contains("bump"),
                "SW013: `{}::{}` (строка {}) объявляет seeds без bump — PDA не проверен",
                f.struct_name,
                f.field,
                f.line
            );
        }
    }
}

// ---------------------------------------------------------------------------
// SW016: init_if_needed только там, где риск принят и обоснован
// ---------------------------------------------------------------------------
#[test]
fn init_if_needed_only_inside_accepted_allowlist() {
    // (struct, field) — единственные места, где init_if_needed принят.
    let allowed: HashSet<(&str, &str)> = [("CreateTask", "mint_reserve")].into_iter().collect();

    let lines: Vec<&str> = SRC.lines().collect();
    for (idx, raw) in lines.iter().enumerate() {
        if !strip_comment(raw).contains("init_if_needed") {
            continue;
        }
        // Имя поля — первое объявление ниже атрибута.
        let mut field = None;
        let mut struct_name = String::new();
        for j in (idx + 1)..(idx + 12).min(lines.len()) {
            if let Some(name) = field_name_of(lines[j]) {
                field = Some(name);
                for k in (0..=j).rev() {
                    if let Some(s) = struct_name_of(lines[k]) {
                        struct_name = s;
                        break;
                    }
                }
                break;
            }
        }
        let field = field.unwrap_or_else(|| {
            panic!("SW016: init_if_needed на строке {} не удалось сопоставить с полем", idx + 1)
        });
        assert!(
            allowed.contains(&(struct_name.as_str(), field.as_str())),
            "SW016: init_if_needed у `{}::{}` (строка {}) вне allowlist'а принятых рисков",
            struct_name,
            field,
            idx + 1
        );

        // Принятый риск обязан быть обоснован прямо в коде, а не только в отчёте.
        let context: String = lines[idx.saturating_sub(8)..=idx].join("\n");
        assert!(
            context.contains("ADR-0015") || context.contains("безопасен"),
            "SW016: у `{}::{}` нет обоснования в комментарии рядом с init_if_needed",
            struct_name,
            field
        );
    }
}

// ---------------------------------------------------------------------------
// SW024: деление на ноль
// ---------------------------------------------------------------------------
#[test]
fn no_unchecked_division_or_remainder() {
    for (idx, raw) in SRC.lines().enumerate() {
        let code = strip_comment(raw);
        if !code.contains('/') && !code.contains('%') {
            continue;
        }
        let code = code.replace('"', "");
        let suspicious = code.contains(" / ") || code.contains('/') && !code.contains("//")
            && !code.contains("::") && !code.contains("///");
        if !suspicious && !code.contains('%') {
            continue;
        }
        let has_guard = code.contains("checked_div")
            || code.contains("checked_rem")
            || code.contains("saturating")
            || code.contains("require!")
            || code.contains("!= 0");
        assert!(
            has_guard,
            "SW024: строка {} содержит деление/остаток без проверки делителя: {}",
            idx + 1,
            code.trim()
        );
    }
}

// ---------------------------------------------------------------------------
// emit! на изменении состояния: без событий хаб слеп
// ---------------------------------------------------------------------------
#[test]
fn state_changing_instructions_emit_events() {
    let allowed_without_event: HashSet<&str> = [
        "init_pool",      // создание конфигурации, отдельного события нет
        "init_profile",   // профиль создаётся отдельной инструкцией
        "claim",          // изменение счётчика, не денежное
        "submit",         // сабмишен — данные, выплаты ещё нет
    ]
    .into_iter()
    .collect();

    let handlers = [
        "pub fn init_pool",
        "pub fn init_profile",
        "pub fn create_task",
        "pub fn claim",
        "pub fn submit",
        "pub fn moderate",
        "pub fn payout",
        "pub fn refund_expired",
        "pub fn payout_rank_bonus",
        "pub fn withdraw_from_pool",
    ];

    for handler in handlers {
        let name = handler.trim_start_matches("pub fn ");
        let start = SRC
            .find(handler)
            .unwrap_or_else(|| panic!("не найден обработчик {}", name));
        let rest = &SRC[start..];
        let end = match rest.find("\n    pub fn ") {
            Some(idx) if idx > 10 => idx,
            _ => rest.len(),
        };
        let body = &rest[..end];
        assert!(
            allowed_without_event.contains(name) || body.contains("emit!"),
            "инструкция `{}` меняет состояние, но не эмитит событие — хаб не увидит факт",
            name
        );
    }
}
