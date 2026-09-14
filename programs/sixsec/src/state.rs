//! Ончейн-состояние SixSec и чистая логика расчётов пула призов.
//!
//! Логика в этом модуле намеренно отделена от инструкций: она не трогает
//! аккаунты и потому покрыта юнит-тестами без валидатора. Это та часть,
//! которую мастер-промпт требует тестировать до мержа.

use anchor_lang::prelude::*;

use crate::error::SixsecError;

/// Максимум тиров награды на одно задание (ADR-0004).
/// Фиксированный массив вместо Vec: размер Anchor-аккаунта обязан быть детерминированным.
pub const MAX_TIERS: usize = 4;

/// Максимальная длина строковых полей, хранимых в аккаунтах.
pub const MAX_URI_LEN: usize = 200;
pub const MAX_REASON_LEN: usize = 140;

/// Тир награды (ADR-0004).
///
/// `token_amount` — в базовых единицах. `decimals` намеренно НЕ хранится:
/// он читается из mint-аккаунта ончейн (ADR-0010), иначе расхождение между
/// объявленным и фактическим decimals дало бы выплату в 10^n раз не туда.
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Default, PartialEq, Eq, Debug)]
pub struct RewardTier {
    pub tier_id: u8,
    pub token_amount: u64,
    pub token_mint: Pubkey,
    pub nft_reward_id: Option<Pubkey>,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, Debug)]
pub enum TaskStatus {
    Open,
    Filled,
    Approved,
    Rejected,
    Expired,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, Debug)]
pub enum ClaimStatus {
    Active,
    Expired,
    Submitted,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, Debug)]
pub enum ModStatus {
    Pending,
    AutoRejected,
    Approved,
    Rejected,
}

#[account]
#[derive(Debug)]
pub struct TaskAccount {
    pub task_id: u64,
    pub creator: Pubkey,
    pub brief_uri: String,
    pub tier_count: u8,
    pub tiers: [RewardTier; MAX_TIERS],
    /// ADR-0003: вместо `claimed_by: Option<Pubkey>` из мастер-промпта.
    pub max_claims: u32,
    pub claim_count: u32,
    /// ADR-0009: сколько зарезервировано в пуле под это задание.
    pub reserved_amount: u64,
    pub status: TaskStatus,
    pub deadline: i64,
}

/// ADR-0003: отдельный аккаунт на пару (task, worker).
/// PDA-сиды гарантируют, что один воркер не возьмёт одно задание дважды.
#[account]
#[derive(Debug)]
pub struct ClaimAccount {
    pub task: Pubkey,
    pub worker: Pubkey,
    pub claimed_at: i64,
    pub expires_at: i64,
    pub status: ClaimStatus,
}

#[account]
#[derive(Debug)]
pub struct SubmissionAccount {
    pub claim: Pubkey,
    pub task: Pubkey,
    pub worker: Pubkey,
    pub media_hash: [u8; 32],
    pub media_uri: String,
    /// Хеш подписанного согласия на передачу прав (раздел 9 промпта).
    pub consent_hash: [u8; 32],
    pub submitted_at: i64,
    pub moderation_status: ModStatus,
    /// ADR-0004: тир выбирает модератор при одобрении, а не задаётся заранее.
    pub awarded_tier_id: Option<u8>,
    pub moderator: Option<Pubkey>,
    pub rejection_reason: Option<String>,
}

/// Учёт резервов пула призов (ADR-0006, ADR-0009).
///
/// Сами токены лежат в PDA-токен-аккаунтах; здесь только бухгалтерия,
/// которую нельзя вывести из баланса токена.
#[account]
#[derive(Debug)]
pub struct PoolState {
    pub admin: Pubkey,
    /// Авторитет модерации (раздел 3 промпта: `moderate` — только admin/multisig).
    /// В проде это адрес мультисига Squads: мультисиг сам подписывает транзакцию,
    /// поэтому сравнение с signer'ом работает и для single-key, и для multisig.
    pub moderator_authority: Pubkey,
    pub total_reserved: u64,
    pub epoch: u64,
    pub withdrawn_this_epoch: u64,
    pub withdrawal_limit: u64,
}

// ---------------------------------------------------------------------------
// Чистая логика
// ---------------------------------------------------------------------------
//
// Эти функции намеренно возвращают `Result<_, SixsecError>`, а не
// `anchor_lang::Result`. Две причины:
//   1. Логика не зависит от обёртки ошибок Anchor и тестируется без неё.
//   2. `anchor_lang::error::Error::to_string()` для кастомной ошибки печатает
//      код (`custom program error: 0x...`), а не имя варианта — сравнивать
//      сообщения в тестах было бы проверкой форматирования, а не поведения.
//      Здесь тесты сверяют сами варианты через `matches!`.

/// `anchor_lang::prelude` экспортирует `Result<T>` — алиас на ОДИН параметр,
/// поэтому `Result<T, SixsecError>` не компилируется (E0107). Свой алиас.
pub type LogicResult<T> = std::result::Result<T, SixsecError>;

/// Худший случай резерва под задание (ADR-0009):
/// `max_claims × max(tiers[*].token_amount)`.
///
/// Точный размер выплаты неизвестен до модерации (тир выбирает модератор,
/// одобрить могут несколько сабмишенов), поэтому резервируется потолок.
///
/// `checked_mul` обязателен: без него переполнение дало бы молча неверный
/// (меньший) резерв, а это ровно тот класс бага, из-за которого пул
/// оказывается необеспеченным.
pub fn worst_case_reserve(
    max_claims: u32,
    tiers: &[RewardTier],
    tier_count: u8,
) -> LogicResult<u64> {
    let count = tier_count as usize;
    if count == 0 || count > tiers.len() {
        return Err(SixsecError::NoTiers);
    }

    let max_amount = tiers[..count]
        .iter()
        .map(|t| t.token_amount)
        .max()
        .unwrap_or(0);

    (max_amount as u128)
        .checked_mul(max_claims as u128)
        .filter(|v| *v <= u64::MAX as u128)
        .map(|v| v as u64)
        .ok_or(SixsecError::ReserveOverflow)
}

/// Свободные средства пула: баланс минус сумма резервов открытых заданий.
pub fn available_balance(pool_balance: u64, total_reserved: u64) -> LogicResult<u64> {
    pool_balance
        .checked_sub(total_reserved)
        .ok_or(SixsecError::PoolBalanceShort)
}

/// Хватает ли свободных средств под новый резерв.
pub fn can_reserve(pool_balance: u64, total_reserved: u64, reserve: u64) -> bool {
    available_balance(pool_balance, total_reserved)
        .map(|free| free >= reserve)
        .unwrap_or(false)
}

/// Проверка, что вывод из пула не ломает резервы открытых заданий (ADR-0009)
/// и не превышает лимит за эпоху (раздел 3 промпта).
pub fn can_withdraw(
    pool_balance: u64,
    total_reserved: u64,
    withdrawn_this_epoch: u64,
    withdrawal_limit: u64,
    amount: u64,
) -> LogicResult<()> {
    let free = available_balance(pool_balance, total_reserved)?;
    if amount > free {
        return Err(SixsecError::WithdrawWouldBreakReserves);
    }
    let after = withdrawn_this_epoch
        .checked_add(amount)
        .ok_or(SixsecError::ReserveOverflow)?;
    if after > withdrawal_limit {
        return Err(SixsecError::WithdrawWouldBreakReserves);
    }
    Ok(())
}

/// Проверка, что tier_id объявлен заданием (ADR-0004).
pub fn validate_tier(
    tier_count: u8,
    tiers: &[RewardTier],
    tier_id: u8,
) -> LogicResult<RewardTier> {
    let count = tier_count as usize;
    if count == 0 || count > tiers.len() {
        return Err(SixsecError::NoTiers);
    }
    if (tier_id as usize) >= count {
        return Err(SixsecError::TierOutOfRange);
    }
    let tier = tiers[tier_id as usize];
    if tier.tier_id != tier_id {
        return Err(SixsecError::TierOutOfRange);
    }
    Ok(tier)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tier(id: u8, amount: u64) -> RewardTier {
        RewardTier {
            tier_id: id,
            token_amount: amount,
            token_mint: Pubkey::new_unique(),
            nft_reward_id: None,
        }
    }

    fn tiers4(a: u64, b: u64, c: u64, d: u64) -> [RewardTier; MAX_TIERS] {
        [tier(0, a), tier(1, b), tier(2, c), tier(3, d)]
    }

    #[test]
    fn reserve_uses_most_expensive_tier_not_first() {
        // Самый дорогой тир — третий. Резерв обязан считаться по нему.
        let t = tiers4(100, 200, 900, 50);
        assert_eq!(worst_case_reserve(5, &t, 3).unwrap(), 4500);
    }

    #[test]
    fn reserve_ignores_tiers_beyond_tier_count() {
        // tier_count = 2: тиры 2 и 3 объявлены в массиве, но не активны.
        let t = tiers4(100, 200, 9_999_999, 8_888_888);
        assert_eq!(worst_case_reserve(3, &t, 2).unwrap(), 600);
    }

    #[test]
    fn reserve_single_claim_single_tier() {
        let t = tiers4(250, 0, 0, 0);
        assert_eq!(worst_case_reserve(1, &t, 1).unwrap(), 250);
    }

    #[test]
    fn reserve_zero_claims_is_zero() {
        let t = tiers4(100, 0, 0, 0);
        assert_eq!(worst_case_reserve(0, &t, 1).unwrap(), 0);
    }

    #[test]
    fn reserve_overflow_is_rejected_not_wrapped() {
        // u64::MAX * 2 переполняет u64. Без checked_mul это молча дало бы
        // u64::MAX - 1 и необеспеченный пул.
        let t = tiers4(u64::MAX, 0, 0, 0);
        assert!(
            matches!(worst_case_reserve(2, &t, 1), Err(SixsecError::ReserveOverflow)),
            "переполнение обязано быть ошибкой, а не молчаливым wrap"
        );
    }

    #[test]
    fn reserve_exact_u64_max_is_allowed() {
        let t = tiers4(u64::MAX, 0, 0, 0);
        assert_eq!(worst_case_reserve(1, &t, 1).unwrap(), u64::MAX);
    }

    #[test]
    fn reserve_requires_at_least_one_tier() {
        let t = tiers4(100, 0, 0, 0);
        assert!(matches!(worst_case_reserve(1, &t, 0), Err(SixsecError::NoTiers)));
    }

    #[test]
    fn available_balance_subtracts_reserves() {
        assert_eq!(available_balance(1000, 400).unwrap(), 600);
    }

    #[test]
    fn available_balance_never_goes_negative() {
        // Резервы превысили баланс (например, пул вывели) — это ошибка,
        // а не 0 и не wrap.
        assert!(matches!(
            available_balance(100, 250),
            Err(SixsecError::PoolBalanceShort)
        ));
    }

    #[test]
    fn can_reserve_boundary_is_inclusive() {
        assert!(can_reserve(1000, 400, 600), "ровно впритык — разрешено");
        assert!(!can_reserve(1000, 400, 601), "на единицу больше — отказ");
    }

    #[test]
    fn can_reserve_false_when_pool_underwater() {
        assert!(!can_reserve(100, 250, 1));
    }

    #[test]
    fn withdraw_blocked_when_it_touches_reserves() {
        assert!(matches!(
            can_withdraw(1000, 900, 0, u64::MAX, 200),
            Err(SixsecError::WithdrawWouldBreakReserves)
        ));
    }

    #[test]
    fn withdraw_blocked_over_epoch_limit() {
        assert!(matches!(
            can_withdraw(1_000_000, 0, 900, 1_000, 200),
            Err(SixsecError::WithdrawWouldBreakReserves)
        ));
    }

    #[test]
    fn withdraw_allowed_within_both_limits() {
        assert!(can_withdraw(1000, 200, 100, 1000, 500).is_ok());
    }

    #[test]
    fn withdraw_overflow_in_epoch_counter_is_rejected() {
        assert!(matches!(
            can_withdraw(u64::MAX, 0, u64::MAX, u64::MAX, 1),
            Err(SixsecError::ReserveOverflow)
        ));
    }

    #[test]
    fn validate_tier_accepts_declared_index() {
        let t = tiers4(100, 200, 300, 400);
        assert_eq!(validate_tier(3, &t, 2).unwrap().token_amount, 300);
    }

    #[test]
    fn validate_tier_rejects_index_beyond_tier_count() {
        // Тир 3 объявлен в массиве, но tier_count = 2, значит он не активен.
        let t = tiers4(100, 200, 300, 400);
        assert!(matches!(
            validate_tier(2, &t, 3),
            Err(SixsecError::TierOutOfRange)
        ));
    }

    #[test]
    fn validate_tier_rejects_mismatched_id() {
        // Индекс валиден, но tier_id внутри не совпадает — битые данные.
        let mut t = tiers4(100, 200, 300, 400);
        t[1].tier_id = 9;
        assert!(matches!(validate_tier(3, &t, 1), Err(SixsecError::TierOutOfRange)));
    }

    #[test]
    fn validate_tier_rejects_zero_tier_count() {
        let t = tiers4(100, 200, 300, 400);
        assert!(matches!(validate_tier(0, &t, 0), Err(SixsecError::NoTiers)));
    }
}
