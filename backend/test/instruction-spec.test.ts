import { test } from "node:test";
import assert from "node:assert/strict";
import { buildModeratePayload, MODERATE_SPEC, PROGRAM_ID } from "../src/chain/instructions.ts";
import { MockChainSource } from "../src/chain/mock.ts";
import type { Decision } from "../src/moderation/authorization.ts";

const queue = await new MockChainSource().getPendingQueue();
const item = queue[0]!;
const MODERATOR = "8SrNQieUhEvgPBi1m4Jq6mzLgCo3V2cNGFutRhiUs3U2";

test("спека описывает пять аккаунтов в порядке программы", () => {
  // Порядок сверяется с IDL в CI (scripts/check_idl_spec.py). Здесь он
  // закреплён, чтобы случайная перестановка в спеке упала сразу, а не в CI.
  assert.deepEqual(
    MODERATE_SPEC.accounts.map((a) => a.name),
    ["moderator", "task", "submission", "pool_state", "worker_profile"],
  );
});

test("флаги mut/signer соответствуют контексту Moderate в lib.rs", () => {
  const by = Object.fromEntries(MODERATE_SPEC.accounts.map((a) => [a.name, a]));
  // #[account(mut)] moderator: Signer — единственный подписант.
  assert.deepEqual([by.moderator!.isMut, by.moderator!.isSigner], [true, true]);
  // task: Account<TaskAccount> — без mut.
  assert.deepEqual([by.task!.isMut, by.task!.isSigner], [false, false]);
  // #[account(mut, constraint=...)] submission
  assert.deepEqual([by.submission!.isMut, by.submission!.isSigner], [true, false]);
  // pool_state — без mut (moderate его не меняет).
  assert.deepEqual([by.pool_state!.isMut, by.pool_state!.isSigner], [false, false]);
  // #[account(mut, seeds=[PROFILE_SEED, worker])] worker_profile
  assert.deepEqual([by.worker_profile!.isMut, by.worker_profile!.isSigner], [true, false]);
  assert.equal(
    MODERATE_SPEC.accounts.filter((a) => a.isSigner).length, 1,
    "подписант ровно один",
  );
});

test("аргументы — Option там, где в программе Option", () => {
  // tier_id: Option<u8>, reason: Option<String>. Это и было источником бага:
  // backend слал 0 и "", то есть «выдать тир 0» вместо «тир не выдавать».
  assert.deepEqual(MODERATE_SPEC.args.map((a) => a.name), ["approve", "tier_id", "reason"]);
  assert.deepEqual(MODERATE_SPEC.args[1]!.type, { option: "u8" });
  assert.deepEqual(MODERATE_SPEC.args[2]!.type, { option: "string" });
  assert.equal(MODERATE_SPEC.args[0]!.type, "bool");
});

test("при одобрении тир задан, причина — null", () => {
  const d: Decision = { kind: "approve", tierId: 2 };
  const p = buildModeratePayload(d, item, MODERATOR);
  assert.equal(p.args.approve, true);
  assert.equal(p.args.tier_id, 2);
  assert.equal(p.args.reason, null, "при одобрении причины быть не должно");
});

test("при отказе тир — null, а не 0", () => {
  const d: Decision = { kind: "reject", reason: "дольше шести секунд" };
  const p = buildModeratePayload(d, item, MODERATOR);
  assert.equal(p.args.approve, false);
  assert.equal(p.args.tier_id, null, "0 означало бы «выдать тир 0»");
  assert.equal(p.args.reason, "дольше шести секунд");
});

test("одобрение нулевым тиром отличимо от отказа", () => {
  const approve0 = buildModeratePayload({ kind: "approve", tierId: 0 }, item, MODERATOR);
  const reject = buildModeratePayload({ kind: "reject", reason: "брак" }, item, MODERATOR);
  assert.equal(approve0.args.tier_id, 0);
  assert.equal(reject.args.tier_id, null);
  assert.notDeepEqual(approve0.args, reject.args);
});

test("список аккаунтов полный, в нужном порядке", () => {
  const p = buildModeratePayload({ kind: "approve", tierId: 0 }, item, MODERATOR);
  assert.equal(p.program, PROGRAM_ID);
  assert.equal(p.accounts.length, 5);
  assert.deepEqual(p.accounts.map((a) => a.name), MODERATE_SPEC.accounts.map((a) => a.name));
});

test("известные адреса подставлены, остальные честно помечены", () => {
  const p = buildModeratePayload({ kind: "approve", tierId: 0 }, item, MODERATOR);
  const by = Object.fromEntries(p.accounts.map((a) => [a.name, a]));
  assert.equal(by.moderator!.pubkey, MODERATOR);
  assert.equal(by.task!.pubkey, item.submission.task);
  assert.equal(by.submission!.pubkey, item.submission.claim);
  // poolState и workerProfile — PDA, backend без чейна их не вычисляет.
  assert.equal(by.pool_state!.pubkey, null);
  assert.equal(by.worker_profile!.pubkey, null);
  assert.deepEqual(p.unresolved, ["pool_state", "worker_profile"]);
  assert.equal(p.signerRequired, MODERATOR);
});

test("пустой unresolved означает полное описание", () => {
  // Инвариант для клиента: если unresolved пуст, транзакцию можно собирать as is.
  const p = buildModeratePayload({ kind: "approve", tierId: 0 }, item, MODERATOR);
  assert.equal(
    p.unresolved.length,
    p.accounts.filter((a) => a.pubkey === null).length,
    "unresolved обязан точно соответствовать незаполненным аккаунтам",
  );
});
