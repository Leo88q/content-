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

/// Резервы под открытые задания **по каждому минту отдельно** (ADR-0015).
///
/// Единый `total_reserved` в `PoolState` был корректен, пока награда платилась
/// одним токеном. Как только в пуле появились два актива (игровой токен + SKR),
/// суммирование их резервов в одно число стало бессмысленной арифметикой:
/// задание, платящее токеном A, видело бы пул «занятым» резервами под токен B.
///
/// PDA `["reserve", mint]`.
#[account]
#[derive(Debug)]
pub struct MintReserve {
    pub mint: Pubkey,
    pub reserved: u64,
}

/// Учёт пула призов (ADR-0006, ADR-0009).
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
    /// Адрес минта SKR для рангового бонуса (ADR-0015). Нулевой pubkey означает,
    /// что гибридный бонус выключен.
    pub skr_mint: Pubkey,
    pub epoch: u64,
    pub withdrawn_this_epoch: u64,
    pub withdrawal_limit: u64,
    /// Лимит объёма SKR-бонусов за эпоху (ADR-0016, Q26).
    ///
    /// `withdrawal_limit` ограничивает вывод админом, но не выплаты воркерам.
    /// Без этого лимита компрометация модератора или баг в `moderate` позволяли
    /// бы выдавать бонусы бесконечно, пока в пуле есть SKR.
    pub skr_payout_limit: u64,
    /// Собственная эпоха счётчика SKR.
    ///
    /// НЕ может переиспользовать `epoch` вывода: оба счётчика читают одни часы,
    /// и если `withdraw_from_pool` отработает в новой эпохе первым, он сдвинет
    /// общий `epoch`, и `skr_paid_this_epoch` уже никогда не сбросится — лимит
    /// на бонусы стал бы пожизненным. У каждого накопительного счётчика своя эпоха.
    pub skr_epoch: u64,
    pub skr_paid_this_epoch: u64,
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
pub fn available_balance(pool_balance: u64, reserved: u64) -> LogicResult<u64> {
    pool_balance
        .checked_sub(reserved)
        .ok_or(SixsecError::PoolBalanceShort)
}

/// Хватает ли свободных средств под новый резерв.
pub fn can_reserve(pool_balance: u64, reserved: u64, reserve: u64) -> bool {
    available_balance(pool_balance, reserved)
        .map(|free| free >= reserve)
        .unwrap_or(false)
}

/// Проверка, что вывод из пула не ломает резервы открытых заданий (ADR-0009)
/// и не превышает лимит за эпоху (раздел 3 промпта).
/// Сброс накопленного счётчика при смене эпохи (ADR-0016).
///
/// Возвращает `(актуальная эпоха, актуальное накопление)`.
///
/// Если `current_epoch` **меньше** сохранённой (откат часов, форк, подмена),
/// счётчик НЕ сбрасывается: сброс по чужим часам — это обход лимита. Считаем
/// смену эпохи только при движении вперёд.
pub fn epoch_accumulator(stored_epoch: u64, current_epoch: u64, accumulated: u64) -> (u64, u64) {
    if current_epoch > stored_epoch {
        (current_epoch, 0)
    } else {
        (stored_epoch, accumulated)
    }
}

pub fn can_withdraw(
    pool_balance: u64,
    reserved: u64,
    withdrawn_this_epoch: u64,
    withdrawal_limit: u64,
    amount: u64,
) -> LogicResult<()> {
    let free = available_balance(pool_balance, reserved)?;
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

// ---------------------------------------------------------------------------
// Игровой слой: профиль воркера и trust score
// ---------------------------------------------------------------------------
//
// On-chain здесь только то, что является границей безопасности (ADR-0011):
// trust_score решает, идёт заявка в полную ручную модерацию или в облегчённую,
// а за модерацией стоит payout из пула призов. Опыт, уровни и дерево навыков
// живут off-chain намеренно.

/// Диапазон trust score.
pub const MAX_TRUST: u16 = 1000;

/// Порог облегчённой модерации. Константа программы, а не настройка бэкенда:
/// иначе его можно поднять одним UPDATE и выпустить брак в контент-пайплайн.
pub const LIGHT_MODERATION_THRESHOLD: u16 = 700;

/// Асимметрия намеренная: набрать рейтинг дороже, чем потерять.
/// Один авто-отклон отбивается только тремя одобрениями (40 / 15 = 2.67).
pub const TRUST_GAIN_APPROVED: u16 = 15;
pub const TRUST_LOSS_REJECTED: u16 = 25;
pub const TRUST_LOSS_AUTO_REJECTED: u16 = 40;

// ---------------------------------------------------------------------------
// Ранговый бонус в SKR (ADR-0015, гибридная модель награды)
// ---------------------------------------------------------------------------

/// Decimals SKR. Взято из вторичного источника и **НЕ проверено ончейн**:
/// публичный RPC из рабочей песочницы недоступен. Проверка обязательна до
/// первой реальной выплаты — ошибка здесь меняет выплату в 10^n раз.
pub const SKR_DECIMALS: u8 = 6;

const SKR_UNIT: u64 = 1_000_000; // 1 SKR при decimals = 6

/// Бонус достаётся только верхним рангам, а не каждому одобрению.
pub const SKR_BONUS_TIER1_THRESHOLD: u16 = 850;
pub const SKR_BONUS_TIER2_THRESHOLD: u16 = 950;

/// 100 SKR (~$2 при курсе ~$0.02) и 250 SKR (~$5).
pub const SKR_BONUS_TIER1: u64 = 100 * SKR_UNIT;
pub const SKR_BONUS_TIER2: u64 = 250 * SKR_UNIT;

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, Debug)]
pub enum ModerationTier {
    /// Каждая заявка проверяется вручную.
    Full,
    /// Авто-префильтр + выборочный ручной контроль.
    Light,
}

/// Профиль воркера. PDA `["profile", worker]`.
///
/// Мутируется ТОЛЬКО в инструкции `moderate` (ADR-0012): если trust score
/// можно изменить из игрового слоя, «прокачка» становится обходом модерации.
#[account]
#[derive(Debug)]
pub struct WorkerProfile {
    pub worker: Pubkey,
    pub trust_score: u16,
    pub approved_count: u32,
    pub rejected_count: u32,
    pub auto_rejected_count: u32,
    pub last_updated: i64,
}

/// Насыщение обязано быть saturating: без него переполнение u16 на 65536-м
/// одобрении обнулило бы рейтинг проверенного воркера.
pub fn trust_after_approval(trust: u16) -> u16 {
    trust.saturating_add(TRUST_GAIN_APPROVED).min(MAX_TRUST)
}

pub fn trust_after_rejection(trust: u16) -> u16 {
    trust.saturating_sub(TRUST_LOSS_REJECTED)
}

pub fn trust_after_auto_rejection(trust: u16) -> u16 {
    trust.saturating_sub(TRUST_LOSS_AUTO_REJECTED)
}

/// Ранговый бонус в SKR по trust score (ADR-0015).
///
/// Намеренно считается от рейтинга, а не от тира задания: бонус — награда за
/// ранг, а не за конкретный бриф. Поэтому он не входит в `RewardTier` и не
/// зависит от того, что заказчик положил в задание.
pub fn rank_bonus_skr(trust: u16) -> u64 {
    if trust >= SKR_BONUS_TIER2_THRESHOLD {
        SKR_BONUS_TIER2
    } else if trust >= SKR_BONUS_TIER1_THRESHOLD {
        SKR_BONUS_TIER1
    } else {
        0
    }
}

pub fn moderation_tier(trust: u16) -> ModerationTier {
    if trust >= LIGHT_MODERATION_THRESHOLD {
        ModerationTier::Light
    } else {
        ModerationTier::Full
    }
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

#[cfg(test)]
mod trust_tests {
    use super::*;

    #[test]
    fn approval_raises_score() {
        assert_eq!(trust_after_approval(500), 515);
    }

    #[test]
    fn approval_saturates_at_max_not_wraps() {
        // Без min(MAX_TRUST) рейтинг ушёл бы за потолок; без saturating_add —
        // переполнился бы и обнулился на 65536-м одобрении.
        assert_eq!(trust_after_approval(MAX_TRUST), MAX_TRUST);
        assert_eq!(trust_after_approval(MAX_TRUST - 5), MAX_TRUST);
    }

    #[test]
    fn rejection_floors_at_zero_not_wraps() {
        assert_eq!(trust_after_rejection(10), 0);
        assert_eq!(trust_after_rejection(0), 0, "u16 не должен заворачиваться в 65511");
    }

    #[test]
    fn auto_rejection_floors_at_zero() {
        assert_eq!(trust_after_auto_rejection(20), 0);
        assert_eq!(trust_after_auto_rejection(0), 0);
    }

    #[test]
    fn auto_rejection_is_harsher_than_manual_rejection() {
        // Явный брак, пойманный префильтром, — более сильный сигнал,
        // чем субъективное решение модератора.
        assert!(TRUST_LOSS_AUTO_REJECTED > TRUST_LOSS_REJECTED);
        assert_eq!(trust_after_auto_rejection(500), 460);
        assert_eq!(trust_after_rejection(500), 475);
    }

    #[test]
    fn one_auto_reject_costs_three_approvals() {
        // 40 / 15 = 2.67 -> нужно три одобрения, чтобы отбить один авто-отклон.
        // Это и есть защита от фарма объёмом.
        let start = 500;
        let after_abuse = trust_after_auto_rejection(start);
        assert_eq!(after_abuse, 460);

        let mut t = after_abuse;
        t = trust_after_approval(t);
        t = trust_after_approval(t);
        assert!(t < start, "двух одобрений недостаточно");
        t = trust_after_approval(t);
        assert!(t >= start, "три одобрения отбивают авто-отклон");
    }

    #[test]
    fn volume_farming_sinks_faster_than_it_climbs() {
        // Фармер чередует брак и годноту 1:1. За цикл рейтинг обязан падать.
        let mut t = 500u16;
        for _ in 0..10 {
            t = trust_after_auto_rejection(t);
            t = trust_after_approval(t);
        }
        assert!(t < 500, "при 1:1 рейтинг должен снижаться, а не держаться");
    }

    #[test]
    fn moderation_tier_boundary_is_inclusive_at_threshold() {
        assert_eq!(
            moderation_tier(LIGHT_MODERATION_THRESHOLD - 1),
            ModerationTier::Full
        );
        assert_eq!(
            moderation_tier(LIGHT_MODERATION_THRESHOLD),
            ModerationTier::Light,
            "ровно на пороге тир уже облегчённый"
        );
    }

    #[test]
    fn fresh_account_cannot_reach_light_tier_by_default() {
        // Из нуля нужно 47 одобрений подряд (700 / 15 = 46.67).
        // Проверяем, что облегчённая модерация недостижима «с наскока».
        let mut t = 0u16;
        for _ in 0..46 {
            t = trust_after_approval(t);
        }
        assert_eq!(moderation_tier(t), ModerationTier::Full);
        t = trust_after_approval(t);
        assert_eq!(moderation_tier(t), ModerationTier::Light);
    }

    #[test]
    fn tier_is_reversible_after_abuse() {
        // Облегчённый тир не даётся навсегда: после серии брака воркер
        // обязан вернуться в полную ручную модерацию.
        let mut t = LIGHT_MODERATION_THRESHOLD;
        assert_eq!(moderation_tier(t), ModerationTier::Light);
        for _ in 0..8 {
            t = trust_after_auto_rejection(t);
        }
        assert_eq!(moderation_tier(t), ModerationTier::Full);
    }
}

#[cfg(test)]
mod skr_bonus_tests {
    use super::*;

    #[test]
    fn no_bonus_below_threshold() {
        assert_eq!(rank_bonus_skr(0), 0);
        assert_eq!(rank_bonus_skr(SKR_BONUS_TIER1_THRESHOLD - 1), 0);
    }

    #[test]
    fn tier1_starts_exactly_at_threshold() {
        assert_eq!(rank_bonus_skr(SKR_BONUS_TIER1_THRESHOLD), SKR_BONUS_TIER1);
    }

    #[test]
    fn tier2_starts_exactly_at_threshold() {
        assert_eq!(
            rank_bonus_skr(SKR_BONUS_TIER2_THRESHOLD - 1),
            SKR_BONUS_TIER1,
            "прямо под вторым порогом ещё первый уровень"
        );
        assert_eq!(rank_bonus_skr(SKR_BONUS_TIER2_THRESHOLD), SKR_BONUS_TIER2);
    }

    #[test]
    fn bonus_is_capped_at_top_rank() {
        assert_eq!(rank_bonus_skr(MAX_TRUST), SKR_BONUS_TIER2);
    }

    #[test]
    fn bonus_does_not_grow_monotonically_with_trust() {
        // Бонус ступенчатый, а не пропорциональный: между 850 и 949 он одинаков.
        assert_eq!(
            rank_bonus_skr(850),
            rank_bonus_skr(949),
            "внутри ступени бонус не меняется"
        );
        assert!(rank_bonus_skr(950) > rank_bonus_skr(949));
    }

    #[test]
    fn amounts_match_declared_decimals() {
        // Защита от рассогласования констант и SKR_DECIMALS.
        assert_eq!(SKR_BONUS_TIER1, 100 * 10u64.pow(SKR_DECIMALS as u32));
        assert_eq!(SKR_BONUS_TIER2, 250 * 10u64.pow(SKR_DECIMALS as u32));
    }

    #[test]
    fn skr_bonus_only_reaches_top_quarter_of_ranks() {
        // Бонус доступен с 850 из 1000: не больше 15% диапазона рейтинга.
        // Это защита от превращения SKR-бонуса в массовую выплату.
        let eligible = (SKR_BONUS_TIER1_THRESHOLD..=MAX_TRUST).count();
        let total = (0..=MAX_TRUST).count();
        assert!(eligible * 100 / total <= 15);
    }
}

#[cfg(test)]
mod epoch_tests {
    use super::*;

    #[test]
    fn first_epoch_is_not_rolled_over() {
        // Инициализация ставит epoch = 0; epoch 0 в часах не должна обнулять счётчик.
        assert_eq!(epoch_accumulator(0, 0, 500), (0, 500));
    }

    #[test]
    fn rolls_over_when_epoch_advances() {
        assert_eq!(epoch_accumulator(5, 6, 999), (6, 0));
        assert_eq!(epoch_accumulator(5, 7, 999), (7, 0), "пропуск эпохи тоже сбрасывает");
    }

    #[test]
    fn same_epoch_keeps_accumulated() {
        assert_eq!(epoch_accumulator(6, 6, 42), (6, 42));
    }

    #[test]
    fn clock_rollback_does_not_reset_limit() {
        // Ключевой кейс: откат часов не должен открывать лимит заново.
        assert_eq!(epoch_accumulator(10, 3, 777), (10, 777));
    }

    #[test]
    fn u64_max_epoch_does_not_panic() {
        assert_eq!(epoch_accumulator(u64::MAX, 0, 5), (u64::MAX, 5));
    }
}
