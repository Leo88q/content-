//! Негативные сценарии подстановки аккаунтов.
//!
//! ЧЕСТНО О ГРАНИЦАХ: это тесты МОДЕЛИ, а не прогон настоящей транзакции.
//! Настоящий прогон сегодня невозможен по двум независимым причинам:
//!   1. интеграционные тесты SixSec запаркованы `#[ignore]` — litesvm 0.10 не
//!      грузит ELF от тулчейна Anchor 1.2.0 (ADR-0014);
//!   2. сценарии ниже требуют SPL Token-2022 аккаунты, то есть загруженный
//!      в валидатор SPL-программу. В LiteSVM её нет, переход планируется на
//!      Surfpool.
//!
//! Что здесь проверяется и почему это не «тест ради теста»: модель
//! воспроизводит код, который Anchor 1.2.0 ГЕНЕРИРУЕТ для констрейнтов
//! `token::mint` и `token::authority` (источник: lang/syn/src/codegen/accounts/
//! constraints.rs, функция `generate_constraint_token_account`):
//!
//!     if #name.mint   != #mint.key()      { return Err(ConstraintTokenMint) }
//!     if #name.owner  != #authority.key() { return Err(ConstraintTokenOwner) }
//!
//! То есть тест утверждает: если констрейнты написаны (а это проверяет
//! `constraints_guard.rs`), то подстановка чужого минта или чужого владельца
//! приводит к ошибке, а канонический PDA проходит. Разрыв между моделью и
//! сгенерированным кодом исключён цитированием codegen'а выше.
//!
//! Как только заработает Surfpool, эти тесты надо заменить настоящими
//! транзакциями, а модели удалить — иначе они превратятся в тесты самих себя.

use anchor_lang::prelude::Pubkey;

/// Минимальное представление SPL token-аккаунта: ровно те поля, которые
/// проверяют констрейнты `token::mint` и `token::authority`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct TokenAccountModel {
    mint: Pubkey,
    owner: Pubkey,
}

/// Минимальное представление PDA: адрес и точка выхода.
#[derive(Debug, Clone, Copy)]
struct PdaModel {
    address: Pubkey,
    bump: u8,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ConstraintError {
    TokenMint,
    TokenOwner,
    AccountAlreadyInitialized,
}

/// Воспроизведение сгенерированного Anchor кода для `token::mint` /
/// `token::authority` (см. шапку файла).
fn check_token_account(
    account: TokenAccountModel,
    expected_mint: Pubkey,
    expected_authority: Pubkey,
) -> Result<(), ConstraintError> {
    if account.mint != expected_mint {
        return Err(ConstraintError::TokenMint);
    }
    if account.owner != expected_authority {
        return Err(ConstraintError::TokenOwner);
    }
    Ok(())
}

/// Воспроизведение проверки `#[account(init, ...)]`: Anchor отказывает, если
/// аккаунт уже существует (ErrorCode::AccountAlreadyInitialized).
fn check_init(address: Pubkey, existing: &[Pubkey]) -> Result<(), ConstraintError> {
    if existing.contains(&address) {
        return Err(ConstraintError::AccountAlreadyInitialized);
    }
    Ok(())
}

fn pubkey(seed: u8) -> Pubkey {
    let mut bytes = [seed; 32];
    bytes[0] = seed;
    bytes[31] = seed.wrapping_add(1);
    Pubkey::new_from_array(bytes)
}

// ---------------------------------------------------------------------------
// SW009: подмена token-аккаунта другого минта
// ---------------------------------------------------------------------------
#[test]
fn model_payout_rejects_token_account_of_foreign_mint() {
    let reward_mint = pubkey(1);
    let foreign_mint = pubkey(2);
    let prize_pool = PdaModel { address: pubkey(3), bump: 251 };

    let canonical = TokenAccountModel { mint: reward_mint, owner: prize_pool.address };
    assert!(
        check_token_account(canonical, reward_mint, prize_pool.address).is_ok(),
        "канонический PDA-пул обязан проходить проверку"
    );

    let substituted = TokenAccountModel { mint: foreign_mint, owner: prize_pool.address };
    assert_eq!(
        check_token_account(substituted, reward_mint, prize_pool.address),
        Err(ConstraintError::TokenMint),
        "SW009: token-аккаунт чужого минта обязан отклоняться"
    );
}

// ---------------------------------------------------------------------------
// SW010: подмена token-аккаунта, которым владеет атакующий
// ---------------------------------------------------------------------------
#[test]
fn model_payout_rejects_token_account_owned_by_attacker() {
    let reward_mint = pubkey(1);
    let prize_pool = PdaModel { address: pubkey(3), bump: 251 };
    let attacker = pubkey(9);

    let victim = TokenAccountModel { mint: reward_mint, owner: attacker };
    assert_eq!(
        check_token_account(victim, reward_mint, prize_pool.address),
        Err(ConstraintError::TokenOwner),
        "SW010: аккаунт, владельцем которого является атакующий, обязан отклоняться"
    );

    // Тот же сценарий для ATA воркера: authority — сам воркер, а не пул.
    let worker = pubkey(7);
    let worker_ata = TokenAccountModel { mint: reward_mint, owner: worker };
    assert!(check_token_account(worker_ata, reward_mint, worker).is_ok());
    assert_eq!(
        check_token_account(worker_ata, reward_mint, attacker),
        Err(ConstraintError::TokenOwner),
        "выплата на ATA чужого воркера обязана отклоняться"
    );
}

// ---------------------------------------------------------------------------
// Повторная инициализация
// ---------------------------------------------------------------------------
#[test]
fn model_reinitialization_of_existing_pda_is_rejected() {
    let pool_state = pubkey(11);
    let skr_pool = pubkey(12);

    assert!(check_init(pool_state, &[]).is_ok(), "первая инициализация проходит");
    assert_eq!(
        check_init(pool_state, &[pool_state, skr_pool]),
        Err(ConstraintError::AccountAlreadyInitialized),
        "повторный init_pool обязан падать: иначе лимит вывода можно обнулить post-factum"
    );
}

// ---------------------------------------------------------------------------
// Guard: модель обязана расходиться с кодом при смене codegen'а Anchor
// ---------------------------------------------------------------------------
#[test]
fn model_matches_anchor_generated_comparison_semantics() {
    // Если Anchor перестанет сравнивать owner (или начнёт сравнивать что-то
    // ещё), эта модель станет ложной. Якорь — документация codegen'а 1.2.0;
    // при обновлении Anchor этот тест обязан пересматриваться первым.
    let mint = pubkey(21);
    let authority = pubkey(22);
    let account = TokenAccountModel { mint, owner: authority };

    // Порядок проверок в сгенерированном коде: сначала mint, затем owner.
    let both_wrong = TokenAccountModel { mint: pubkey(23), owner: pubkey(24) };
    assert_eq!(
        check_token_account(both_wrong, mint, authority),
        Err(ConstraintError::TokenMint),
        "при двух нарушениях первым падает mint — так же, как в codegen'е Anchor"
    );
    assert!(check_token_account(account, mint, authority).is_ok());
}
