import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AuditLog, parseAuditLine, decisionsBy } from "../src/moderation/audit.ts";
import { MockChainSource, MODERATOR } from "../src/chain/mock.ts";
import { queueItemContext } from "../src/moderation/audit.ts";

const source = new MockChainSource();
const queue = await source.getPendingQueue();
const item = queue[0]!;

async function tempLog(): Promise<{ log: AuditLog; path: string }> {
  const dir = await mkdtemp(join(tmpdir(), "sixsec-audit-"));
  const path = join(dir, "nested", "audit.jsonl");
  return { log: new AuditLog(path), path };
}

test("запись дописывается, а не перезаписывает файл", async () => {
  const { log, path } = await tempLog();
  await log.record({ moderator: MODERATOR, ...queueItemContext(item), decision: { kind: "approve", tierId: 1 }, txSignature: "", reviewDurationMs: 1200 });
  await log.record({ moderator: MODERATOR, ...queueItemContext(item), decision: { kind: "reject", reason: "брак" }, txSignature: "", reviewDurationMs: 400 });
  const raw = await readFile(path, "utf8");
  const lines = raw.trim().split("\n");
  assert.equal(lines.length, 2, "обе записи на месте");
});

test("каждая строка — валидный JSON (JSONL читается построчно)", async () => {
  const { log, path } = await tempLog();
  await log.record({ moderator: MODERATOR, ...queueItemContext(item), decision: { kind: "reject", reason: "первая\nстрока" }, txSignature: "", reviewDurationMs: null });
  const raw = await readFile(path, "utf8");
  const lines = raw.trim().split("\n");
  assert.equal(lines.length, 1, "перенос строки в причине не должен ломать JSONL");
  const parsed = parseAuditLine(lines[0]!);
  assert.ok(parsed);
  assert.equal(parsed.decision.kind, "reject");
});

test("битая строка не роняет разбор всего лога", () => {
  assert.equal(parseAuditLine("{не json"), null);
  assert.equal(parseAuditLine(""), null);
  assert.equal(parseAuditLine("   "), null);
  assert.equal(parseAuditLine("42"), null);
  assert.ok(parseAuditLine('{"moderator":"x"}'));
});

test("метка времени проставляется, если не задана", async () => {
  const { log } = await tempLog();
  const e = await log.record({ moderator: MODERATOR, ...queueItemContext(item), decision: { kind: "approve", tierId: 0 }, txSignature: "", reviewDurationMs: null });
  assert.ok(!Number.isNaN(Date.parse(e.at)), "at обязан быть валидным ISO-8601");
});

test("выборка по модератору", async () => {
  const { log } = await tempLog();
  const a = await log.record({ moderator: MODERATOR, ...queueItemContext(item), decision: { kind: "approve", tierId: 0 }, txSignature: "", reviewDurationMs: 10 });
  const b = await log.record({ moderator: "Other1111111111111111111111111111111111111", ...queueItemContext(item), decision: { kind: "approve", tierId: 0 }, txSignature: "", reviewDurationMs: 10 });
  assert.equal(decisionsBy([a, b], MODERATOR).length, 1);
  assert.equal(decisionsBy([a, b], "Nobody1111111111111111111111111111111111111").length, 0);
});

test("в аудит пишется режим модерации — иначе порог 700 не оценить", () => {
  const ctx = queueItemContext(item);
  assert.equal(ctx.moderationTier, item.moderationTier);
  assert.equal(ctx.worker, item.submission.worker);
});
