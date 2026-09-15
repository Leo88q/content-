import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp, parseDecision, devAuthenticator, jsonReplacer } from "../src/api/server.ts";
import { MockChainSource, MODERATOR } from "../src/chain/mock.ts";
import { AuditLog } from "../src/moderation/audit.ts";

const handler = createApp({
  chain: new MockChainSource(),
  audit: new AuditLog(join(await mkdtemp(join(tmpdir(), "sixsec-api-")), "audit.jsonl")),
  auth: devAuthenticator,
  mockData: true,
});

/** Минимальный заменитель IncomingMessage/ServerResponse без поднятия сокета. */
function call(method: string, url: string, body?: unknown, headers: Record<string, string> = {}) {
  const chunks: Buffer[] = [];
  if (body !== undefined) chunks.push(Buffer.from(JSON.stringify(body)));
  const req: any = {
    method,
    url,
    headers: { "content-type": "application/json", ...headers },
    async *[Symbol.asyncIterator]() {
      for (const c of chunks) yield c;
    },
  };
  let status = 0;
  const out: Buffer[] = [];
  const res: any = {
    writeHead(s: number) { status = s; },
    end(payload?: string | Buffer) { if (payload) out.push(Buffer.from(payload as string)); },
  };
  return handler(req, res).then(() => ({
    status,
    text: Buffer.concat(out).toString("utf8"),
    json: () => JSON.parse(Buffer.concat(out).toString("utf8")),
  }));
}

test("health отдаёт признак фикстур", async () => {
  const r = await call("GET", "/api/health");
  assert.equal(r.status, 200);
  assert.equal(r.json().mockData, true);
});

test("очередь содержит оба режима модерации", async () => {
  const r = await call("GET", "/api/queue");
  assert.equal(r.status, 200);
  const b = r.json();
  assert.equal(b.moderatorAuthority, MODERATOR);
  assert.ok(b.counts.full > 0);
  assert.ok(b.counts.light > 0);
  assert.equal(b.counts.total, b.counts.full + b.counts.light);
});

test("u64-поля уходят строкой, а не числом", async () => {
  const r = await call("GET", "/api/queue");
  // Number теряет точность выше 2^53, поэтому bigint обязан уходить строкой.
  // Проверяются поля, которые этот эндпоинт действительно возвращает.
  assert.match(r.text, /"tokenAmount":"\d+"/);
  assert.match(r.text, /"reservedAmount":"\d+"/);
  assert.match(r.text, /"deadline":"\d+"/);
  assert.match(r.text, /"submittedAt":"\d+"/);

  const b = r.json();
  const item = b.items[0];
  for (const field of ["reservedAmount", "deadline"] as const) {
    assert.equal(typeof item.task[field], "string", `${field} обязан быть строкой`);
  }
  assert.equal(typeof item.submission.submittedAt, "string");
  assert.equal(typeof item.task.tiers[0].tokenAmount, "string");
});

test("сериализация u64 не теряет разряды выше 2^53", () => {
  // Реальная проверка риска: значение за пределами Number.MAX_SAFE_INTEGER.
  // 2^63 + 1, а не просто 2^63: степень двойки точно представима в double,
  // и на ней потеря разряда не видна. Теряется именно «нестепенное» значение.
  const big = 2n ** 63n + 1n;
  assert.ok(big > BigInt(Number.MAX_SAFE_INTEGER), "фикстур обязан превышать 2^53");
  const out = JSON.stringify({ reserve: big }, jsonReplacer);
  assert.equal(out, '{"reserve":"9223372036854775809"}');
  assert.equal(BigInt(JSON.parse(out).reserve), big, "значение пережило round-trip");
  // Контроль: наивное приведение к Number на этом же значении уже врёт,
  // поэтому строковая сериализация — не косметика.
  assert.notEqual(
    BigInt(Number(big)),
    big,
    "ожидание: Number теряет разряд, иначе тест ничего не доказывает",
  );
});

test("без заголовка модератора — 401", async () => {
  const r = await call("POST", "/api/decisions", { claim: "clm1", decision: { kind: "approve", tierId: 0 } });
  assert.equal(r.status, 401);
  assert.equal(r.json().error.code, "Unauthenticated");
});

test("чужой кошелёк — 403, и данные очереди не раскрываются", async () => {
  const r = await call(
    "POST", "/api/decisions",
    { claim: "clm1", decision: { kind: "approve", tierId: 0 } },
    { "x-moderator": "41KGWZBVfe75zsvdHQwTZ3w3PBK13atKihchB86NdVXv" },
  );
  assert.equal(r.status, 403);
  assert.equal(r.json().error.code, "NotModeratorAuthority");
});

test("модератор одобряет, но решение возвращается НЕ подписанным", async () => {
  const r = await call(
    "POST", "/api/decisions",
    { claim: "clm1", decision: { kind: "approve", tierId: 1 } },
    { "x-moderator": MODERATOR },
  );
  assert.equal(r.status, 202);
  const b = r.json();
  assert.equal(b.signed, false, "backend не имеет права подписывать");
  assert.equal(b.instruction.name, "moderate");
  assert.equal(b.instruction.args.approve, true);
  assert.equal(b.instruction.args.tierId, 1);
  assert.equal(b.instruction.args.reason, null, "при одобрении причина отсутствует");
  assert.equal(b.instruction.signerRequired, MODERATOR);
  // Полный список аккаунтов: кошелёк не сможет собрать транзакцию без него.
  assert.equal(b.instruction.accounts.length, 5);
  assert.deepEqual(
    b.instruction.accounts.map((a: { name: string }) => a.name),
    ["moderator", "task", "submission", "poolState", "workerProfile"],
  );
});

test("несуществующий тир — 422, не 403", async () => {
  const r = await call(
    "POST", "/api/decisions",
    { claim: "clm2", decision: { kind: "approve", tierId: 99 } },
    { "x-moderator": MODERATOR },
  );
  assert.equal(r.status, 422);
  assert.equal(r.json().error.code, "InvalidTierId");
});

test("отказ без причины — 422", async () => {
  const r = await call(
    "POST", "/api/decisions",
    { claim: "clm3", decision: { kind: "reject", reason: "  " } },
    { "x-moderator": MODERATOR },
  );
  assert.equal(r.status, 422);
  assert.equal(r.json().error.code, "EmptyRejectionReason");
});

test("несуществующий сабмишен — 404", async () => {
  const r = await call(
    "POST", "/api/decisions",
    { claim: "nope", decision: { kind: "approve", tierId: 0 } },
    { "x-moderator": MODERATOR },
  );
  assert.equal(r.status, 404);
});

test("мусор в теле — 400, а не падение", async () => {
  for (const bad of [
    { claim: 123, decision: { kind: "approve", tierId: 0 } },
    { claim: "clm1" },
    { claim: "clm1", decision: { kind: "approve", tierId: "0" } },
    { claim: "clm1", decision: { kind: "reject" } },
    { claim: "clm1", decision: { kind: "whatever" } },
  ]) {
    const r = await call("POST", "/api/decisions", bad, { "x-moderator": MODERATOR });
    assert.equal(r.status, 400, `ожидался 400 для ${JSON.stringify(bad)}`);
  }
});

test("неизвестный маршрут — 404 JSON, не HTML-заглушка", async () => {
  const r = await call("GET", "/api/nope");
  assert.equal(r.status, 404);
  assert.equal(r.json().error.code, "NotFound");
});

test("parseDecision отвергает всё непроверенное", () => {
  assert.equal(parseDecision(null), null);
  assert.equal(parseDecision("approve"), null);
  assert.equal(parseDecision({ kind: "approve" }), null);
  assert.equal(parseDecision({ kind: "approve", tierId: 1.5 }), null);
  assert.equal(parseDecision({ kind: "reject", reason: 5 }), null);
  assert.deepEqual(parseDecision({ kind: "approve", tierId: 2 }), { kind: "approve", tierId: 2 });
});
