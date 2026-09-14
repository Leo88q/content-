//! SixSec — маркетплейс микрозаданий для генерации промо-контента.
//!
//! Принципы, зафиксированные ADR:
//! - ADR-0003: M параллельных сабмишенов на задание, `ClaimAccount` на пару (task, worker).
//! - ADR-0004: награда — тиры, конкретный тир выбирает модератор при одобрении.
//! - ADR-0006: единый пул призов, пополняется вручную вне программы.
//!   **Программа не держит mint authority** и физически не может создать
//!   награду, которой не существует.
//! - ADR-0009: резерв по худшему случаю при `create_task`, повторная проверка
//!   баланса в `payout`, событие нехватки для казначейства.
//! - ADR-0010: mint награды — аргумент, а не константа; `decimals` читается из
//!   mint-аккаунта ончейн; mint с активным freeze authority отклоняется.

use anchor_lang::prelude::*;
use anchor_spl::token_interface::{
    self, Mint, TokenAccount, TokenInterface, TransferChecked,
};

pub mod error;
pub mod state;

use error::SixsecError;
use state::*;

declare_id!("SixSec1111111111111111111111111111111111111");

/// Длительность лока claim'а по умолчанию (секунды). ADR-0003.
pub const DEFAULT_CLAIM_LOCK_SECS: i64 = 6 * 60 * 60;

pub const POOL_STATE_SEED: &[u8] = b"pool_state";
pub const PRIZE_POOL_SEED: &[u8] = b"prize_pool";
pub const TASK_SEED: &[u8] = b"task";
pub const CLAIM_SEED: &[u8] = b"claim";
pub const SUBMISSION_SEED: &[u8] = b"submission";
pub const PROFILE_SEED: &[u8] = b"profile";

/// Пространство аккаунтов: 8 (дискриминатор) + размер структуры + запас на String.
const TASK_SPACE: usize = 8 + 8 + 32 + (4 + MAX_URI_LEN) + 1
    + (MAX_TIERS * (1 + 8 + 32 + 1 + 32))
    + 4 + 4 + 8 + 1 + 8;
const CLAIM_SPACE: usize = 8 + 32 + 32 + 8 + 8 + 1;
const SUBMISSION_SPACE: usize = 8 + 32 + 32 + 32 + 32 + (4 + MAX_URI_LEN) + 32
    + 8 + 1 + (1 + 1) + (1 + 32) + (1 + 4 + MAX_REASON_LEN);
const POOL_STATE_SPACE: usize = 8 + 32 + 32 + 8 + 8 + 8 + 8;
const PROFILE_SPACE: usize = 8 + 32 + 2 + 4 + 4 + 4 + 8;

#[program]
pub mod sixsec {
    use super::*;

    /// Инициализация учёта резервов пула. `admin` обязан быть мультисигом —
    /// single-key админа в проде мастер-промпт запрещает.
    pub fn init_pool(
        ctx: Context<InitPool>,
        withdrawal_limit: u64,
        moderator_authority: Pubkey,
    ) -> Result<()> {
        let pool = &mut ctx.accounts.pool_state;
        pool.admin = ctx.accounts.admin.key();
        pool.moderator_authority = moderator_authority;
        pool.total_reserved = 0;
        pool.epoch = 0;
        pool.withdrawn_this_epoch = 0;
        pool.withdrawal_limit = withdrawal_limit;
        Ok(())
    }

    /// Создание профиля воркера. Отдельная инструкция, а НЕ `init_if_needed`
    /// внутри `claim` — при ленивой инициализации воркер мог бы сбросить себе
    /// trust_score повторным взятием задания, то есть обнулить последствия
    /// авто-отклонений. Здесь повторный вызов падает, потому что PDA занят.
    pub fn init_profile(ctx: Context<InitProfile>) -> Result<()> {
        let profile = &mut ctx.accounts.worker_profile;
        profile.worker = ctx.accounts.worker.key();
        profile.trust_score = 0;
        profile.approved_count = 0;
        profile.rejected_count = 0;
        profile.auto_rejected_count = 0;
        profile.last_updated = Clock::get()?.unix_timestamp;
        Ok(())
    }

    /// Создание задания. Резервирует в пуле средства по худшему случаю
    /// (ADR-0009) и проверяет mint награды ончейн (ADR-0010).
    pub fn create_task(
        ctx: Context<CreateTask>,
        task_id: u64,
        brief_uri: String,
        tiers: Vec<RewardTier>,
        max_claims: u32,
        deadline: i64,
    ) -> Result<()> {
        require!(brief_uri.len() <= MAX_URI_LEN, SixsecError::FieldTooLong);
        require!(!tiers.is_empty() && tiers.len() <= MAX_TIERS, SixsecError::NoTiers);

        let clock = Clock::get()?;
        require!(deadline > clock.unix_timestamp, SixsecError::TaskExpired);

        // ADR-0010: mint награды проверяется по факту, а не по доверию.
        // decimals берётся из mint-аккаунта; freeze authority недопустима.
        let reward_mint = &ctx.accounts.reward_mint;
        require!(
            reward_mint.freeze_authority.is_none(),
            SixsecError::RewardMintFreezable
        );
        let decimals = reward_mint.decimals;

        let tier_count = tiers.len() as u8;
        let mut tier_arr = [RewardTier::default(); MAX_TIERS];
        for (i, t) in tiers.iter().enumerate() {
            require!(t.tier_id == i as u8, SixsecError::TierOutOfRange);
            require!(t.token_mint == reward_mint.key(), SixsecError::TierOutOfRange);
            tier_arr[i] = *t;
        }

        let reserve = worst_case_reserve(max_claims, &tier_arr, tier_count)?;

        // ADR-0009: резерв по худшему случаю против свободных средств пула.
        let pool_balance = ctx.accounts.prize_pool.amount;
        let pool_state = &ctx.accounts.pool_state;
        require!(
            can_reserve(pool_balance, pool_state.total_reserved, reserve),
            SixsecError::InsufficientPoolReserve
        );

        // КРИТИЧНО: резерв обязан накапливаться в пуле, а не только на задании.
        // Без этой строки total_reserved навсегда остаётся нулём, can_reserve
        // всегда проходит, и каждое следующее задание резервирует весь пул
        // заново — ровно тот сценарий, против которого написан ADR-0009.
        let pool_state = &mut ctx.accounts.pool_state;
        pool_state.total_reserved = pool_state
            .total_reserved
            .checked_add(reserve)
            .ok_or(SixsecError::ReserveOverflow)?;

        let task = &mut ctx.accounts.task;
        task.task_id = task_id;
        task.creator = ctx.accounts.creator.key();
        task.brief_uri = brief_uri;
        task.tier_count = tier_count;
        task.tiers = tier_arr;
        task.max_claims = max_claims;
        task.claim_count = 0;
        task.reserved_amount = reserve;
        task.status = TaskStatus::Open;
        task.deadline = deadline;

        emit!(TaskCreated {
            task: task.key(),
            task_id,
            max_claims,
            reserve,
            decimals,
        });
        Ok(())
    }

    /// Взятие задания воркером. ADR-0003: до `max_claims` параллельно.
    pub fn claim(ctx: Context<Claim>) -> Result<()> {
        let task = &mut ctx.accounts.task;
        let clock = Clock::get()?;

        require!(
            task.status == TaskStatus::Open,
            SixsecError::NoClaimsLeft
        );
        require!(
            clock.unix_timestamp < task.deadline,
            SixsecError::TaskExpired
        );
        require!(
            task.claim_count < task.max_claims,
            SixsecError::NoClaimsLeft
        );

        task.claim_count = task
            .claim_count
            .checked_add(1)
            .ok_or(SixsecError::ReserveOverflow)?;
        if task.claim_count == task.max_claims {
            task.status = TaskStatus::Filled;
        }

        let claim = &mut ctx.accounts.claim;
        claim.task = task.key();
        claim.worker = ctx.accounts.worker.key();
        claim.claimed_at = clock.unix_timestamp;
        claim.expires_at = clock
            .unix_timestamp
            .checked_add(DEFAULT_CLAIM_LOCK_SECS)
            .ok_or(SixsecError::ReserveOverflow)?;
        claim.status = ClaimStatus::Active;

        Ok(())
    }

    /// Отправка сабмишена. `consent_hash` — хеш подписанного согласия на
    /// передачу прав (раздел 9 промпта): делает согласие ончейн-доказуемым.
    pub fn submit(
        ctx: Context<Submit>,
        media_hash: [u8; 32],
        media_uri: String,
        consent_hash: [u8; 32],
    ) -> Result<()> {
        require!(media_uri.len() <= MAX_URI_LEN, SixsecError::FieldTooLong);

        let claim = &mut ctx.accounts.claim;
        let clock = Clock::get()?;

        require!(claim.status == ClaimStatus::Active, SixsecError::ClaimExpired);
        require!(
            clock.unix_timestamp < claim.expires_at,
            SixsecError::ClaimExpired
        );
        claim.status = ClaimStatus::Submitted;

        let sub = &mut ctx.accounts.submission;
        sub.claim = claim.key();
        sub.task = ctx.accounts.task.key();
        sub.worker = claim.worker;
        sub.media_hash = media_hash;
        sub.media_uri = media_uri;
        sub.consent_hash = consent_hash;
        sub.submitted_at = clock.unix_timestamp;
        sub.moderation_status = ModStatus::Pending;
        sub.awarded_tier_id = None;
        sub.moderator = None;
        sub.rejection_reason = None;

        Ok(())
    }

    /// Решение модератора. При одобрении выбирается тир (ADR-0004).
    pub fn moderate(
        ctx: Context<Moderate>,
        approve: bool,
        tier_id: Option<u8>,
        reason: Option<String>,
    ) -> Result<()> {
        // Раздел 3 промпта: `moderate` доступна только admin/multisig-авторитету.
        // Без этой проверки одобрить сабмишен и назначить себе тир мог бы кто угодно.
        require!(
            ctx.accounts.moderator.key() == ctx.accounts.pool_state.moderator_authority,
            SixsecError::UnauthorizedModerator
        );

        let sub = &mut ctx.accounts.submission;
        require!(
            sub.moderation_status == ModStatus::Pending,
            SixsecError::AlreadyModerated
        );

        sub.moderator = Some(ctx.accounts.moderator.key());

        // ADR-0011/0012: trust_score меняется ТОЛЬКО здесь. Это единственная
        // точка, где игровой рейтинг соприкасается с ончейн-состоянием.
        let profile = &mut ctx.accounts.worker_profile;
        require!(
            profile.worker == sub.worker,
            SixsecError::ProfileWorkerMismatch
        );
        let clock = Clock::get()?;

        if !approve {
            sub.moderation_status = ModStatus::Rejected;
            if let Some(r) = reason {
                require!(r.len() <= MAX_REASON_LEN, SixsecError::FieldTooLong);
                sub.rejection_reason = Some(r);
            }
            profile.rejected_count = profile.rejected_count.saturating_add(1);
            profile.trust_score = trust_after_rejection(profile.trust_score);
            profile.last_updated = clock.unix_timestamp;
            emit!(SubmissionRejected {
                submission: sub.key(),
                worker: sub.worker,
                trust_score: profile.trust_score,
                moderation_tier: moderation_tier(profile.trust_score) == ModerationTier::Light,
            });
            return Ok(());
        }

        let tid = tier_id.ok_or(SixsecError::TierOutOfRange)?;
        let task = &ctx.accounts.task;
        // ADR-0004: валидируем, что тир объявлен заданием.
        let tier = validate_tier(task.tier_count, &task.tiers, tid)?;

        sub.moderation_status = ModStatus::Approved;
        sub.awarded_tier_id = Some(tid);

        profile.approved_count = profile.approved_count.saturating_add(1);
        profile.trust_score = trust_after_approval(profile.trust_score);
        profile.last_updated = clock.unix_timestamp;

        emit!(SubmissionApproved {
            submission: sub.key(),
            worker: sub.worker,
            task: task.key(),
            tier_id: tid,
            token_amount: tier.token_amount,
            trust_score: profile.trust_score,
        });
        Ok(())
    }

    /// Выплата из пула призов (ADR-0006). Перевод, не минт.
    pub fn payout(ctx: Context<Payout>) -> Result<()> {
        let sub = &ctx.accounts.submission;
        require!(
            sub.moderation_status == ModStatus::Approved,
            SixsecError::TierOutOfRange
        );
        let tier_id = sub.awarded_tier_id.ok_or(SixsecError::TierOutOfRange)?;
        let task = &ctx.accounts.task;
        let tier = validate_tier(task.tier_count, &task.tiers, tier_id)?;

        // ADR-0009: резерв не заменяет проверку фактического баланса.
        if ctx.accounts.prize_pool.amount < tier.token_amount {
            emit!(PoolShortfall {
                mint: ctx.accounts.reward_mint.key(),
                required: tier.token_amount,
                available: ctx.accounts.prize_pool.amount,
            });
            return Err(SixsecError::PoolBalanceShort.into());
        }

        // Привязка к let обязательна: `key()` возвращает временное значение,
        // и без биндинга оно умирает до использования seeds (E0716).
        let mint_key = ctx.accounts.reward_mint.key();
        let seeds = &[
            PRIZE_POOL_SEED,
            mint_key.as_ref(),
            &[ctx.bumps.prize_pool],
        ];
        let signer = [&seeds[..]];

        token_interface::transfer_checked(
            CpiContext::new_with_signer(
                // Anchor 1.2.0: new_with_signer принимает Pubkey, а не AccountInfo
                // (breaking change относительно 0.32 — lang/src/context.rs:198).
                *ctx.accounts.token_program.to_account_info().key,
                TransferChecked {
                    from: ctx.accounts.prize_pool.to_account_info(),
                    mint: ctx.accounts.reward_mint.to_account_info(),
                    to: ctx.accounts.worker_ata.to_account_info(),
                    authority: ctx.accounts.prize_pool.to_account_info(),
                },
                &signer,
            ),
            tier.token_amount,
            ctx.accounts.reward_mint.decimals,
        )?;

        let pool_state = &mut ctx.accounts.pool_state;
        // Резерв задания уменьшается на фактически выплаченное.
        if task.reserved_amount >= tier.token_amount {
            pool_state.total_reserved = pool_state
                .total_reserved
                .saturating_sub(tier.token_amount);
        }

        emit!(PayoutCompleted {
            submission: sub.key(),
            worker: sub.worker,
            mint: ctx.accounts.reward_mint.key(),
            amount: tier.token_amount,
        });
        Ok(())
    }

    /// Освобождение резерва истёкшего задания. Без этой инструкции (её не было
    /// в мастер-промпте) зарезервированные средства зависали бы навсегда.
    pub fn refund_expired(ctx: Context<RefundExpired>) -> Result<()> {
        let task = &mut ctx.accounts.task;
        let clock = Clock::get()?;
        require!(
            clock.unix_timestamp >= task.deadline,
            SixsecError::TaskNotExpired
        );

        task.status = TaskStatus::Expired;
        let released = task.reserved_amount;
        task.reserved_amount = 0;

        let pool_state = &mut ctx.accounts.pool_state;
        pool_state.total_reserved = pool_state.total_reserved.saturating_sub(released);

        emit!(ReserveReleased {
            task: task.key(),
            released,
        });
        Ok(())
    }

    /// Вывод из пула. Только admin/multisig, с учётом резервов и лимита за эпоху
    /// (ADR-0009, раздел 3 промпта).
    pub fn withdraw_from_pool(ctx: Context<WithdrawFromPool>, amount: u64) -> Result<()> {
        let pool_state = &mut ctx.accounts.pool_state;

        can_withdraw(
            ctx.accounts.prize_pool.amount,
            pool_state.total_reserved,
            pool_state.withdrawn_this_epoch,
            pool_state.withdrawal_limit,
            amount,
        )?;

        // Привязка к let обязательна: `key()` возвращает временное значение,
        // и без биндинга оно умирает до использования seeds (E0716).
        let mint_key = ctx.accounts.reward_mint.key();
        let seeds = &[
            PRIZE_POOL_SEED,
            mint_key.as_ref(),
            &[ctx.bumps.prize_pool],
        ];
        let signer = [&seeds[..]];

        token_interface::transfer_checked(
            CpiContext::new_with_signer(
                // Anchor 1.2.0: new_with_signer принимает Pubkey, а не AccountInfo
                // (breaking change относительно 0.32 — lang/src/context.rs:198).
                *ctx.accounts.token_program.to_account_info().key,
                TransferChecked {
                    from: ctx.accounts.prize_pool.to_account_info(),
                    mint: ctx.accounts.reward_mint.to_account_info(),
                    to: ctx.accounts.destination.to_account_info(),
                    authority: ctx.accounts.prize_pool.to_account_info(),
                },
                &signer,
            ),
            amount,
            ctx.accounts.reward_mint.decimals,
        )?;

        pool_state.withdrawn_this_epoch = pool_state
            .withdrawn_this_epoch
            .checked_add(amount)
            .ok_or(SixsecError::ReserveOverflow)?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Account contexts
// ---------------------------------------------------------------------------

#[derive(Accounts)]
pub struct InitPool<'info> {
    #[account(mut)]
    pub admin: Signer<'info>,
    #[account(
        init,
        payer = admin,
        space = POOL_STATE_SPACE,
        seeds = [POOL_STATE_SEED],
        bump
    )]
    pub pool_state: Account<'info, PoolState>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
#[instruction(task_id: u64)]
pub struct CreateTask<'info> {
    #[account(mut)]
    pub creator: Signer<'info>,
    #[account(
        init,
        payer = creator,
        space = TASK_SPACE,
        seeds = [TASK_SEED, task_id.to_le_bytes().as_ref()],
        bump
    )]
    pub task: Account<'info, TaskAccount>,
    #[account(mut, seeds = [POOL_STATE_SEED], bump)]
    pub pool_state: Account<'info, PoolState>,
    /// ADR-0010: mint награды — аргумент, проверяется ончейн.
    pub reward_mint: InterfaceAccount<'info, Mint>,
    #[account(
        seeds = [PRIZE_POOL_SEED, reward_mint.key().as_ref()],
        bump
    )]
    pub prize_pool: InterfaceAccount<'info, TokenAccount>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct InitProfile<'info> {
    #[account(mut)]
    pub worker: Signer<'info>,
    #[account(
        init,
        payer = worker,
        space = PROFILE_SPACE,
        seeds = [PROFILE_SEED, worker.key().as_ref()],
        bump
    )]
    pub worker_profile: Account<'info, WorkerProfile>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Claim<'info> {
    #[account(mut)]
    pub worker: Signer<'info>,
    #[account(mut)]
    pub task: Account<'info, TaskAccount>,
    #[account(
        init,
        payer = worker,
        space = CLAIM_SPACE,
        seeds = [CLAIM_SEED, task.key().as_ref(), worker.key().as_ref()],
        bump
    )]
    pub claim: Account<'info, ClaimAccount>,
    /// Профиль должен существовать до взятия задания (см. init_profile).
    #[account(seeds = [PROFILE_SEED, worker.key().as_ref()], bump, has_one = worker)]
    pub worker_profile: Account<'info, WorkerProfile>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Submit<'info> {
    #[account(mut)]
    pub worker: Signer<'info>,
    pub task: Account<'info, TaskAccount>,
    #[account(
        mut,
        has_one = worker,
        constraint = claim.task == task.key()
    )]
    pub claim: Account<'info, ClaimAccount>,
    #[account(
        init,
        payer = worker,
        space = SUBMISSION_SPACE,
        seeds = [SUBMISSION_SEED, claim.key().as_ref()],
        bump
    )]
    pub submission: Account<'info, SubmissionAccount>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Moderate<'info> {
    #[account(mut)]
    pub moderator: Signer<'info>,
    pub task: Account<'info, TaskAccount>,
    #[account(
        mut,
        constraint = submission.task == task.key()
    )]
    pub submission: Account<'info, SubmissionAccount>,
    /// Авторитет модерации. В проде — мультисиг, не single key.
    pub pool_state: Account<'info, PoolState>,
    #[account(mut, seeds = [PROFILE_SEED, submission.worker.as_ref()], bump)]
    pub worker_profile: Account<'info, WorkerProfile>,
}

#[derive(Accounts)]
pub struct Payout<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    pub task: Account<'info, TaskAccount>,
    #[account(
        constraint = submission.task == task.key(),
        constraint = submission.moderation_status == ModStatus::Approved
    )]
    pub submission: Account<'info, SubmissionAccount>,
    #[account(mut, seeds = [POOL_STATE_SEED], bump)]
    pub pool_state: Account<'info, PoolState>,
    pub reward_mint: InterfaceAccount<'info, Mint>,
    #[account(
        mut,
        seeds = [PRIZE_POOL_SEED, reward_mint.key().as_ref()],
        bump
    )]
    pub prize_pool: InterfaceAccount<'info, TokenAccount>,
    #[account(mut, constraint = worker_ata.owner == submission.worker)]
    pub worker_ata: InterfaceAccount<'info, TokenAccount>,
    pub token_program: Interface<'info, TokenInterface>,
}

#[derive(Accounts)]
pub struct RefundExpired<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(mut)]
    pub task: Account<'info, TaskAccount>,
    #[account(mut, seeds = [POOL_STATE_SEED], bump)]
    pub pool_state: Account<'info, PoolState>,
}

#[derive(Accounts)]
pub struct WithdrawFromPool<'info> {
    #[account(mut, constraint = admin.key() == pool_state.admin @ SixsecError::WithdrawWouldBreakReserves)]
    pub admin: Signer<'info>,
    #[account(mut, seeds = [POOL_STATE_SEED], bump)]
    pub pool_state: Account<'info, PoolState>,
    pub reward_mint: InterfaceAccount<'info, Mint>,
    #[account(mut, seeds = [PRIZE_POOL_SEED, reward_mint.key().as_ref()], bump)]
    pub prize_pool: InterfaceAccount<'info, TokenAccount>,
    #[account(mut, constraint = destination.owner == admin.key())]
    pub destination: InterfaceAccount<'info, TokenAccount>,
    pub token_program: Interface<'info, TokenInterface>,
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

#[event]
pub struct TaskCreated {
    pub task: Pubkey,
    pub task_id: u64,
    pub max_claims: u32,
    pub reserve: u64,
    pub decimals: u8,
}

#[event]
pub struct SubmissionApproved {
    pub submission: Pubkey,
    pub worker: Pubkey,
    pub task: Pubkey,
    pub tier_id: u8,
    pub token_amount: u64,
    pub trust_score: u16,
}

#[event]
pub struct SubmissionRejected {
    pub submission: Pubkey,
    pub worker: Pubkey,
    pub trust_score: u16,
    /// false = воркер остался в полной ручной модерации (ADR-0011).
    pub moderation_tier: bool,
}

#[event]
pub struct PayoutCompleted {
    pub submission: Pubkey,
    pub worker: Pubkey,
    pub mint: Pubkey,
    pub amount: u64,
}

/// ADR-0009: операционный сигнал казначейству, а не аварийная остановка.
#[event]
pub struct PoolShortfall {
    pub mint: Pubkey,
    pub required: u64,
    pub available: u64,
}

#[event]
pub struct ReserveReleased {
    pub task: Pubkey,
    pub released: u64,
}
