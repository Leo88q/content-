//! Интеграционные тесты SixSec на LiteSVM.
//!
//! Шаблон взят из собственного шаблона Anchor 1.2.0 (`cli/src/template.rs`,
//! `create_program_template_litesvm_test_v1`), поэтому набор импортов и способ
//! загрузки `.so` совпадают с тем, что Anchor ожидает от проекта на 1.x.
//!
//! Требует предварительно собранной программы: в CI `anchor build` стоит ПЕРЕД
//! `cargo test`, иначе `include_bytes!` не найдёт `target/deploy/sixsec.so`.

use {
    anchor_lang::{
        prelude::Pubkey,
        solana_program::{instruction::Instruction, system_program},
        AccountDeserialize, InstructionData, ToAccountMetas,
    },
    litesvm::LiteSVM,
    solana_keypair::Keypair,
    solana_message::{Message, VersionedMessage},
    solana_signer::Signer,
    solana_transaction::versioned::VersionedTransaction,
};

const POOL_STATE_SEED: &[u8] = b"pool_state";
const WITHDRAWAL_LIMIT: u64 = 1_000_000;

struct Ctx {
    svm: LiteSVM,
    admin: Keypair,
    moderator: Pubkey,
    pool_state: Pubkey,
}

fn setup() -> Ctx {
    let program_id = sixsec::id();
    let mut svm = LiteSVM::new();
    let bytes = include_bytes!(concat!(
        env!("CARGO_TARGET_TMPDIR"),
        "/../deploy/sixsec.so"
    ));
    svm.add_program(program_id, bytes)
        .expect("программа должна грузиться в LiteSVM");

    let admin = Keypair::new();
    svm.airdrop(&admin.pubkey(), 10_000_000_000)
        .expect("airdrop админу");

    let pool_state = Pubkey::find_program_address(&[POOL_STATE_SEED], &program_id).0;

    Ctx {
        svm,
        admin,
        // Намеренно отдельный ключ: модератор и админ — разные роли,
        // и это различие должно переживать инициализацию.
        moderator: Keypair::new().pubkey(),
        pool_state,
    }
}

fn init_pool_ix(ctx: &Ctx) -> Instruction {
    Instruction::new_with_bytes(
        sixsec::id(),
        &sixsec::instruction::InitPool {
            withdrawal_limit: WITHDRAWAL_LIMIT,
            moderator_authority: ctx.moderator,
        }
        .data(),
        sixsec::accounts::InitPool {
            admin: ctx.admin.pubkey(),
            pool_state: ctx.pool_state,
            system_program: system_program::ID,
        }
        .to_account_metas(None),
    )
}

fn send(svm: &mut LiteSVM, ix: Instruction, payer: &Keypair) -> bool {
    let blockhash = svm.latest_blockhash();
    let msg = Message::new_with_blockhash(&[ix], Some(&payer.pubkey()), &blockhash);
    let tx =
        VersionedTransaction::try_new(VersionedMessage::Legacy(msg), &[payer]).expect("подписывается");
    svm.send_transaction(tx).is_ok()
}

fn read_pool(svm: &LiteSVM, pool_state: &Pubkey) -> sixsec::state::PoolState {
    let account = svm
        .get_account(pool_state)
        .expect("PoolState должен существовать");
    let mut data: &[u8] = &account.data;
    sixsec::state::PoolState::try_deserialize(&mut data).expect("десериализуется")
}

#[test]
fn init_pool_creates_account_with_zero_reserves() {
    let mut ctx = setup();
    // Инструкция строится отдельным statement: иначе ctx заимствуется и как &mut
    // (через ctx.svm), и как & (через init_pool_ix) в одном выражении — E0502.
    let ix = init_pool_ix(&ctx);
    assert!(send(&mut ctx.svm, ix, &ctx.admin), "init_pool должен пройти");

    let pool = read_pool(&ctx.svm, &ctx.pool_state);
    assert_eq!(pool.admin, ctx.admin.pubkey());
    assert_eq!(pool.withdrawal_limit, WITHDRAWAL_LIMIT);
    assert_eq!(pool.total_reserved, 0, "резервов ещё нет");
    assert_eq!(pool.withdrawn_this_epoch, 0);
    assert_eq!(pool.epoch, 0);
}

#[test]
fn init_pool_keeps_moderator_distinct_from_admin() {
    // Раздел 3 промпта: `moderate` доступна только admin/multisig-авторитету.
    // Если модератор молча провалится в админа, проверка авторитета в `moderate`
    // станет декоративной — этот тест фиксирует, что роли не сливаются.
    let mut ctx = setup();
    assert_ne!(
        ctx.moderator,
        ctx.admin.pubkey(),
        "предусловие теста: разные ключи"
    );
    let ix = init_pool_ix(&ctx);
    assert!(send(&mut ctx.svm, ix, &ctx.admin));

    let pool = read_pool(&ctx.svm, &ctx.pool_state);
    assert_eq!(pool.moderator_authority, ctx.moderator);
    assert_eq!(pool.admin, ctx.admin.pubkey());
    assert_ne!(pool.moderator_authority, pool.admin);
}

#[test]
fn init_pool_twice_is_rejected() {
    // PDA один на программу: повторная инициализация не должна перезаписывать
    // казначейские параметры (иначе лимит вывода можно обнулить post-factum).
    let mut ctx = setup();
    let ix = init_pool_ix(&ctx);
    assert!(send(&mut ctx.svm, ix.clone(), &ctx.admin));
    assert!(
        !send(&mut ctx.svm, ix, &ctx.admin),
        "повторный init_pool обязан быть отклонён"
    );

    let pool = read_pool(&ctx.svm, &ctx.pool_state);
    assert_eq!(pool.withdrawal_limit, WITHDRAWAL_LIMIT, "параметры целы");
}
