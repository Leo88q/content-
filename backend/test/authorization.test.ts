import { test } from "node:test";
import assert from "node:assert/strict";
import {
  authorizeDecision,
  assertCanModerate,
  validateDecision,
  MAX_REJECTION_REASON_LEN,
  type Decision,
} from "../src/moderation/authorization.ts";
import { MockChainSource, MODERATOR } from "../src/chain/mock.ts";
import { moderationTier, LIGHT_MODERATION_THRESHOLD } from "../src/domain.ts";

const source = new MockChainSource();
const queue = await source.getPendingQueue();
const first = queue[0]!;
const approve: Decision = { kind: "approve", tierId: 0 };
const reject: Decision = { kind: "reject", reason: "Клип длиннее 6 секунд" };

test("модератор из PoolState проходит проверку", async () => {
  const pool = await source.getPoolState();
  assert.equal(assertCanModerate(MODERATOR, pool.moderatorAuthority).ok, true);
});

test("чужой кошелёк не может принять решение", () => {
  const r = assertCanModerate("3iag8HgokPynPxHWvY3pPHrDjJt68SgufTmqceA9YEAb", MODERATOR);
  assert.equal(r.ok, false);
  if (!r.ok) assert.equal(r.error.code, "NotModeratorAuthority");
});

test("сравнение адресов регистрозависимо", () => {
  // base58 регистрозависим; нормализация открыла бы обход похожим адресом.
  const mutated = MODERATOR[0]!.toLowerCase() + MODERATOR.slice(1);
  if (mutated === MODERATOR) return; // защита от вырожденного фикстура
  assert.equal(assertCanModerate(mutated, MODERATOR).ok, false);
});

test("approve с существующим тиром проходит", () => {
  assert.equal(validateDecision(approve, first).ok, true);
});

test("approve с несуществующим тиром отклоняется", () => {
  const r = validateDecision({ kind: "approve", tierId: first.task.tierCount }, first);
  assert.equal(r.ok, false);
  if (!r.ok) {
    assert.equal(r.error.code, "InvalidTierId");
    if (r.error.code === "InvalidTierId") {
      assert.equal(r.error.tierCount, first.task.tierCount);
    }
  }
});

test("approve с отрицательным тиром отклоняется", () => {
  const r = validateDecision({ kind: "approve", tierId: -1 }, first);
  assert.equal(r.ok, false);
  if (!r.ok) assert.equal(r.error.code, "InvalidTierId");
});

test("последний допустимый тир проходит", () => {
  const last = first.task.tierCount - 1;
  assert.equal(validateDecision({ kind: "approve", tierId: last }, first).ok, true);
});

test("reject без причины отклоняется", () => {
  const r = validateDecision({ kind: "reject", reason: "" }, first);
  assert.equal(r.ok, false);
  if (!r.ok) assert.equal(r.error.code, "EmptyRejectionReason");
});

test("reject из одних пробелов отклоняется", () => {
  const r = validateDecision({ kind: "reject", reason: "   \n\t " }, first);
  assert.equal(r.ok, false);
  if (!r.ok) assert.equal(r.error.code, "EmptyRejectionReason");
});

test("reject с валидной причиной проходит", () => {
  assert.equal(validateDecision(reject, first).ok, true);
});

test("слишком длинная причина отклоняется", () => {
  const r = validateDecision(
    { kind: "reject", reason: "x".repeat(MAX_REJECTION_REASON_LEN + 1) },
    first,
  );
  assert.equal(r.ok, false);
  if (!r.ok) {
    assert.equal(r.error.code, "RejectionReasonTooLong");
    if (r.error.code === "RejectionReasonTooLong") {
      assert.equal(r.error.max, MAX_REJECTION_REASON_LEN);
    }
  }
});

test("граница длины причины включительно", () => {
  assert.equal(
    validateDecision({ kind: "reject", reason: "x".repeat(MAX_REJECTION_REASON_LEN) }, first).ok,
    true,
  );
});

test("решение по уже решённому сабмишену отклоняется", () => {
  const decided = {
    ...first,
    submission: { ...first.submission, moderationStatus: "Approved" as const },
  };
  const r = validateDecision(approve, decided);
  assert.equal(r.ok, false);
  if (!r.ok) assert.equal(r.error.code, "AlreadyDecided");
});

test("auto-rejected сабмишен тоже нельзя пересмотреть этой инструкцией", () => {
  const decided = {
    ...first,
    submission: { ...first.submission, moderationStatus: "AutoRejected" as const },
  };
  const r = validateDecision(reject, decided);
  assert.equal(r.ok, false);
  if (!r.ok) assert.equal(r.error.code, "AlreadyDecided");
});

test("проверка права идёт раньше валидности решения", () => {
  // Иначе неавторизованный вызывающий изучал бы очередь по кодам ошибок:
  // AlreadyDecided и InvalidTierId раскрывают содержимое.
  const decided = {
    ...first,
    submission: { ...first.submission, moderationStatus: "Approved" as const },
  };
  const r = authorizeDecision("41KGWZBVfe75zsvdHQwTZ3w3PBK13atKihchB86NdVXv", MODERATOR, approve, decided);
  assert.equal(r.ok, false);
  if (!r.ok) {
    assert.equal(
      r.error.code,
      "NotModeratorAuthority",
      "ожидалась ошибка авторизации, а не утечка данных о решении",
    );
  }
});

test("полный путь: авторизованный модератор + валидное решение", () => {
  assert.equal(authorizeDecision(MODERATOR, MODERATOR, approve, first).ok, true);
  assert.equal(authorizeDecision(MODERATOR, MODERATOR, reject, first).ok, true);
});

test("фикстуры покрывают оба режима модерации", async () => {
  const tiers = new Set(queue.map((i) => i.moderationTier));
  assert.ok(tiers.has("Full"), "нужен новичок для полной модерации");
  assert.ok(tiers.has("Light"), "нужен работник выше порога для облегчённой");
});

test("порог 700 работает как в state.rs", () => {
  assert.equal(moderationTier(LIGHT_MODERATION_THRESHOLD), "Light");
  assert.equal(moderationTier(LIGHT_MODERATION_THRESHOLD - 1), "Full");
});
