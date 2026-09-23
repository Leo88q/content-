//! Инварианты экономики SixSec как property-тесты (требование `c-07`).
//!
//! Это НЕ интеграционные тесты и НЕ подмена им: исполнение инструкций без
//! валидатора проверить нельзя (ADR-0014). Здесь проверяется то, что
//! проверяемо сегодня и что в шести из десяти случаев и есть источник бага —
//! чистая экономическая логика из `state.rs` плюс модели охранников
//! инструкций.
//!
//! Что такое «модель охранника»: повторяем проверку, которой инструкция
//! защищена (`require!` в `claim`, `payout`, `payout_rank_bonus`,
//! `withdraw_from_pool`), и утверждаем инвариант на длинной последовательности
//! операций. Модель обязана расходиться с кодом один в один — поэтому рядом с
//! каждой моделью указано, какую строку `lib.rs` она повторяет. Если инструкцию
//! поменяют, а модель — нет, тест надо обновить ВМЕСТЕ с инструкцией.
//!
//! Генератор значений детерминирован (LCG): падение обязано воспроизводиться
//! от прогона к прогону, иначе «property-тест» превращается в лотерею.

use sixsec::state::{
    available_balance, can_reserve, can_withdraw, epoch_accumulator, rank_bonus_skr,
    trust_after_approval, trust_after_auto_rejection, trust_after_rejection, validate_tier,
    worst_case_reserve, LogicResult, RewardTier, MAX_TIERS, MAX_TRUST,
};
use sixsec::error::SixsecError;

// ---------------------------------------------------------------------------
// Детерминированный генератор
// ---------------------------------------------------------------------------
struct Lcg(u64);

impl Lcg {
    fn new(seed: u64) -> Self {
        Lcg(seed ^ 0x9E37_79B9_7F4A_7C15)
    }
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1_442_695_040_888_963_407);
        self.0
    }
    /// Значение в [0, max).
    fn below(&mut self, max: u64) -> u64 {
        if max == 0 {
            0
        } else {
            self.next_u64() % max
        }
    }
    /// Значение в [min, max].
    fn range(&mut self, min: u64, max: u64) -> u64 {
        if max <= min {
            min
        } else {
            min + self.below(max - min + 1)
        }
    }
}

fn tier(id: u8, amount: u64) -> RewardTier {
    RewardTier {
        tier_id: id,
        token_amount: amount,
        token_mint: anchor_lang::prelude::Pubkey::default(),
        nft_reward_id: None,
    }
}

fn tiers(count: usize, amounts: &[u64]) -> Vec<RewardTier> {
    (0..count)
        .map(|i| tier(i as u8, amounts.get(i).copied().unwrap_or(0)))
        .collect()
}

// ---------------------------------------------------------------------------
// Инвариант 1: сохранение ценности — balance = available + reserved
// ---------------------------------------------------------------------------
#[test]
fn conservation_available_plus_reserved_equals_balance() {
    let mut rng = Lcg::new(0xC0FFEE);
    for _ in 0..5_000 {
        let balance = rng.range(0, 1_000_000);
        let reserved = rng.range(0, balance);
        let available = available_balance(balance, reserved).expect("резерв в пределах баланса");
        assert_eq!(
            available + reserved,
            balance,
            "нарушено сохранение: balance {} != available {} + reserved {}",
            balance,
            available,
            reserved
        );
    }
}

#[test]
fn conservation_rejects_reserve_beyond_balance_instead_of_going_negative() {
    let mut rng = Lcg::new(0xBADBED);
    for _ in 0..1_000 {
        let balance = rng.range(0, 10_000);
        let reserved = balance + 1 + rng.below(10_000);
        assert!(
            matches!(available_balance(balance, reserved), Err(SixsecError::PoolBalanceShort)),
            "резервы сверх баланса обязаны быть ошибкой, а не отрицательным доступным остатком"
        );
        assert!(!can_reserve(balance, reserved, 1));
    }
}

// ---------------------------------------------------------------------------
// Инвариант 2: резерв по худшему случаю покрывает любую комбинацию выплат
// ---------------------------------------------------------------------------
#[test]
fn worst_case_reserve_covers_every_possible_payout_combination() {
    let mut rng = Lcg::new(0x51A5EC);
    for _ in 0..3_000 {
        let tier_count = (rng.below(MAX_TIERS as u64) + 1) as usize;
        let mut amounts = Vec::new();
        for _ in 0..tier_count {
            amounts.push(rng.range(0, 100_000));
        }
        let max_claims = (rng.below(25) + 1) as u32;
        let t = tiers(tier_count, &amounts);
        let reserve = worst_case_reserve(max_claims, &t, tier_count as u8)
            .expect("значения подобраны без переполнения");

        let max_tier = amounts.iter().copied().max().unwrap_or(0);
        assert!(
            reserve >= max_tier,
            "резерв {} обязан покрывать самую дорогую одиночную выплату {}",
            reserve,
            max_tier
        );

        // Худший сценарий: все слоты заняли и всем одобрили максимальный тир.
        let total = (max_tier as u128) * (max_claims as u128);
        assert!(
            (reserve as u128) >= total,
            "резерв {} не покрывает {} выплат по {}",
            reserve,
            max_claims,
            max_tier
        );

        // Любая реальная комбинация выплат (каждому — свой тир) тоже покрыта.
        let mut paid: u128 = 0;
        for i in 0..max_claims {
            paid += amounts[(i as usize) % tier_count] as u128;
        }
        assert!((reserve as u128) >= paid, "резерв {} не покрывает сценарий {}", reserve, paid);
    }
}

#[test]
fn reserve_is_monotone_in_tier_amounts() {
    let base = [10u64, 20, 30, 40];
    let t = tiers(4, &base);
    let base_reserve = worst_case_reserve(3, &t, 4).unwrap();

    let mut raised = base;
    for idx in 0..4 {
        raised[idx] += 100;
        let t2 = tiers(4, &raised);
        let reserve2 = worst_case_reserve(3, &t2, 4).unwrap();
        assert!(
            reserve2 >= base_reserve,
            "увеличение тира {} не должно уменьшать резерв",
            idx
        );
        raised[idx] -= 100;
    }
}

// ---------------------------------------------------------------------------
// Инвариант 3: вывод не трогает резервы и не пробивает лимит эпохи
// ---------------------------------------------------------------------------
#[test]
fn withdraw_never_touches_reserves_and_never_exceeds_epoch_limit() {
    let mut rng = Lcg::new(0x0D17_0D17);
    for _ in 0..5_000 {
        let balance = rng.range(0, 1_000_000);
        let reserved = rng.range(0, balance);
        let limit = rng.range(0, 100_000);
        let already = rng.range(0, limit);
        let amount = rng.range(0, 200_000);

        let ok = can_withdraw(balance, reserved, already, limit, amount).is_ok();
        if ok {
            let free = balance - reserved;
            assert!(
                amount <= free,
                "вывод {} превышает свободный остаток {}",
                amount,
                free
            );
            assert!(
                already + amount <= limit,
                "вывод {} пробивает лимит эпохи {} (уже {})",
                amount,
                limit,
                already
            );
        }
    }
}

/// Модель `withdraw_from_pool` (lib.rs: epoch_accumulator + can_withdraw).
#[test]
fn epoch_cap_model_total_withdrawn_per_epoch_never_exceeds_limit() {
    let mut rng = Lcg::new(0xE70C_4A9);
    let limit: u64 = 50_000;
    let balance: u64 = 1_000_000;
    let reserved: u64 = 0;

    let mut stored_epoch = 0u64;
    let mut withdrawn: u64 = 0;
    let mut current_epoch = 0u64;
    let mut paid_in_epoch: u64 = 0;

    for step in 0..20_000 {
        // Скачок часов: вперёд, иногда назад (откат/форк — не должен открывать лимит).
        if step % 97 == 0 {
            current_epoch = current_epoch.saturating_add(1);
        }
        if step % 1009 == 0 && current_epoch > 3 {
            current_epoch -= 2;
        }

        let (epoch, accumulated) = epoch_accumulator(stored_epoch, current_epoch, withdrawn);
        if epoch != stored_epoch {
            paid_in_epoch = 0;
        }
        stored_epoch = epoch;
        withdrawn = accumulated;

        let amount = rng.range(0, 5_000);
        if can_withdraw(balance, reserved, withdrawn, limit, amount).is_ok() {
            withdrawn += amount;
            paid_in_epoch += amount;
        }
        assert!(
            paid_in_epoch <= limit,
            "за эпоху {} выведено {}, лимит {}",
            stored_epoch,
            paid_in_epoch,
            limit
        );
        assert!(withdrawn <= limit, "накопленный счётчик вывода превысил лимит");
    }
}

/// Модель `payout_rank_bonus` (лимит SKR-бонусов за эпоху, ADR-0016).
#[test]
fn skr_bonus_cap_model_never_exceeds_epoch_limit() {
    let mut rng = Lcg::new(0x5EED_0001);
    let payout_limit: u64 = 10_000_000;
    let mut stored_epoch = 0u64;
    let mut paid: u64 = 0;

    for step in 0..20_000 {
        let current_epoch = step / 500;
        let (epoch, accumulated) = epoch_accumulator(stored_epoch, current_epoch, paid);
        stored_epoch = epoch;
        paid = accumulated;

        let trust = (rng.below(MAX_TRUST as u64 + 1)) as u16;
        let bonus = rank_bonus_skr(trust);
        if bonus == 0 {
            continue; // require!(bonus > 0)
        }
        match paid.checked_add(bonus) {
            Some(after) if after <= payout_limit => paid = after,
            _ => assert!(
                paid <= payout_limit,
                "лимит SKR-бонусов пробит: {} > {}",
                paid,
                payout_limit
            ),
        }
        assert!(paid <= payout_limit, "SKR за эпоху {} превысил лимит", stored_epoch);
    }
}

// ---------------------------------------------------------------------------
// Инвариант 4: невозможность двойного claim и двойной выплаты
// ---------------------------------------------------------------------------
/// Модель охранника `claim`: `require!(task.claim_count < task.max_claims)`
/// плюс `checked_add(1)`.
#[test]
fn claim_counter_never_exceeds_max_claims() {
    let mut rng = Lcg::new(0xC1A1_0001);
    for _ in 0..2_000 {
        let max_claims = rng.below(50) as u32;
        let mut claimed: u32 = 0;
        let mut attempts = 0u32;
        // Пытаемся занять слотов вдвое больше, чем существует.
        while attempts < max_claims.saturating_mul(2) + 10 {
            attempts += 1;
            if claimed < max_claims {
                claimed = claimed.checked_add(1).expect("u32 не переполняется здесь");
            }
        }
        assert!(
            claimed <= max_claims,
            "счётчик claim {} превысил лимит задания {}",
            claimed,
            max_claims
        );
        assert_eq!(claimed, max_claims, "все слоты должны быть исчерпаны");
    }
}

/// Модель `payout`: повторная выплата по одному сабмишену невозможна.
/// В программе это статус модерации + PDA сабмишена на пару (claim, worker).
#[test]
fn double_payout_model_second_payout_always_rejected() {
    #[derive(PartialEq, Eq, Debug, Clone, Copy)]
    enum ModStatus {
        Pending,
        Approved,
        Paid,
    }

    for _ in 0..2_000 {
        let mut status = ModStatus::Pending;
        let mut payouts: u64 = 0;

        for _ in 0..8 {
            // moderate -> Approved (повторно модерировать уже рассмотренное нельзя)
            if status == ModStatus::Pending {
                status = ModStatus::Approved;
                continue;
            }
            // payout: require!(moderation_status == Approved)
            if status == ModStatus::Approved {
                payouts += 1;
                status = ModStatus::Paid;
                continue;
            }
            // Любая повторная попытка — отклонение.
            assert_eq!(status, ModStatus::Paid);
        }
        assert_eq!(
            payouts, 1,
            "по сабмишену должна пройти ровно одна выплата, прошло {}",
            payouts
        );
    }
}

// ---------------------------------------------------------------------------
// Инвариант 5: платёжеспособность — резерв никогда не превышает баланс
// ---------------------------------------------------------------------------
/// Модель жизненного цикла пула: create_task (резерв по худшему случаю) →
/// payout (уменьшение резерва на выплаченное) → refund_expired (освобождение).
#[test]
fn solvency_model_reserves_never_exceed_pool_balance() {
    let mut rng = Lcg::new(0x5011_0033);
    let pool_balance: u64 = 1_000_000;
    let mut reserved: u64 = 0;
    let mut paid_total: u64 = 0;

    for step in 0..10_000 {
        if step % 3 == 0 {
            // create_task: резерв только если он помещается в свободный остаток.
            let tier_count = (rng.below(MAX_TIERS as u64) + 1) as usize;
            let mut amounts = vec![0u64; tier_count];
            for a in amounts.iter_mut() {
                *a = rng.range(0, 1_000);
            }
            let max_claims = (rng.below(5) + 1) as u32;
            let t = tiers(tier_count, &amounts);
            let reserve = worst_case_reserve(max_claims, &t, tier_count as u8).unwrap();
            if can_reserve(pool_balance, reserved, reserve) {
                reserved += reserve;
            }
        } else if step % 3 == 1 {
            // payout: выплата не больше резерва (иначе это необеспеченный пул).
            let payout = if reserved == 0 {
                0
            } else {
                rng.range(0, reserved.min(2_000) + 1)
            };
            reserved = reserved.saturating_sub(payout); // как в программе
            paid_total = paid_total.saturating_add(payout);
        } else {
            // refund_expired: освобождение остатка задания.
            let released = if reserved == 0 { 0 } else { rng.range(0, reserved + 1) };
            reserved = reserved.saturating_sub(released);
        }
        assert!(
            reserved <= pool_balance,
            "резерв {} превысил баланс пула {} — пул необеспечен",
            reserved,
            pool_balance
        );
    }
    assert!(
        paid_total <= pool_balance * 10_000,
        "совокупные выплаты не должны превышать масштаб пула"
    );
}

// ---------------------------------------------------------------------------
// Инвариант 6: отсутствие отрицательных балансов и переполнений в рейтинге
// ---------------------------------------------------------------------------
#[test]
fn trust_score_never_wraps_and_never_exceeds_max() {
    for t in 0..=u16::MAX {
        let up = trust_after_approval(t);
        let down = trust_after_rejection(t);
        let auto = trust_after_auto_rejection(t);
        assert!(up <= MAX_TRUST, "рейтинг {} после одобрения выше потолка: {}", t, up);
        assert!(down <= t, "отклонение не может повышать рейтинг");
        assert!(auto <= t, "авто-отклонение не может повышать рейтинг");
        // При t > MAX_TRUST (плохие данные в аккаунте) рейтинг обязан упираться
        // в потолок, поэтому сравнение «не ниже исходного» там не применимо.
        assert!(
            up >= t || t >= MAX_TRUST,
            "одобрение не может понижать рейтинг до насыщения (t={}, up={})",
            t,
            up
        );
        // Насыщение сверху: не «обнуление» переполнением.
        if t >= MAX_TRUST.saturating_sub(15) {
            assert_eq!(up, MAX_TRUST, "рядом с потолком рейтинг обязан упираться в MAX_TRUST");
        }
        // Пол снизу: не u16::MAX.
        if t <= 40 {
            assert_eq!(auto, 0, "малый рейтинг обязан упираться в ноль, а не заворачиваться");
        }
    }
}

#[test]
fn rank_bonus_is_monotone_and_bounded() {
    let mut previous = 0u64;
    for t in 0..=MAX_TRUST {
        let bonus = rank_bonus_skr(t);
        assert!(bonus >= previous, "бонус не должен убывать с ростом рейтинга");
        assert!(bonus <= 250 * 10u64.pow(6), "бонус выше максимальной ступени");
        previous = bonus;
    }
}

// ---------------------------------------------------------------------------
// Инвариант 7: эпохи не откатываются назад (сброс по чужим часам = обход лимита)
// ---------------------------------------------------------------------------
#[test]
fn epoch_accumulator_never_resets_when_clock_moves_backwards() {
    let mut rng = Lcg::new(0xE70C_0001);
    for _ in 0..10_000 {
        let stored = rng.range(0, 1_000);
        let accumulated = rng.range(0, 100_000);
        let current = rng.range(0, 1_000);
        let (epoch, acc) = epoch_accumulator(stored, current, accumulated);
        if current <= stored {
            assert_eq!(
                (epoch, acc),
                (stored, accumulated),
                "откат часов не должен сбрасывать накопительный счётчик"
            );
        } else {
            assert_eq!((epoch, acc), (current, 0), "движение часов вперёд обязано сбрасывать счётчик");
        }
    }
}

// ---------------------------------------------------------------------------
// Инвариант 8: тир выплаты всегда объявлен заданием
// ---------------------------------------------------------------------------
#[test]
fn payout_tier_is_always_declared_by_the_task() {
    let mut rng = Lcg::new(0x71E5_0001);
    for _ in 0..5_000 {
        let tier_count = (rng.below(MAX_TIERS as u64) + 1) as usize;
        let mut amounts = vec![0u64; MAX_TIERS];
        for a in amounts.iter_mut() {
            *a = rng.range(1, 1_000);
        }
        let t = tiers(MAX_TIERS, &amounts);
        let requested = rng.below(MAX_TIERS as u64 + 2) as u8;

        let result: LogicResult<RewardTier> = validate_tier(tier_count as u8, &t, requested);
        if (requested as usize) < tier_count {
            let tier = result.expect("объявленный тир обязан приниматься");
            assert_eq!(tier.tier_id, requested, "выплата по чужому тиру");
        } else {
            assert!(
                matches!(result, Err(SixsecError::TierOutOfRange)),
                "тир {} вне tier_count {} обязан отклоняться",
                requested,
                tier_count
            );
        }
    }
}
