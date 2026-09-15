import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, sign, type KeyObject } from "node:crypto";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp, siwsAuthenticator } from "../src/api/server.ts";
import { SiwsVerifier, buildMessage, pubkeyToBase58, type SiwsParams } from "../src/auth/siws.ts";
import { base58Encode } from "../src/auth/base58.ts";
import { MockChainSource } from "../src/chain/mock.ts";
import { AuditLog } from "../src/moderation/audit.ts";
import type { ChainSource } from "../src/chain/source.ts";
import type { PoolState } from "../src/domain.ts";

const siwsParams: SiwsParams = {
  domain: "sixsec.test",
  uri: "http://localhost:8787",
  nonceTtlMs: 60_000,
  sessionTtlMs: 60_000,
};

/**
 * Ключ генерируется в тесте, а не лежит в репозитории: приватные ключи в Git
 * не попадают (правило мастер-промпта, раздел 0). Поэтому модераторский адрес
 * подставляется через собственный ChainSource.
 */
function wallet() {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const raw = new Uint8Array(publicKey.export({ format: "der", type: "spki" }).subarray(12));
  return { b58: pubkeyToBase58(raw), privateKey };
}

function chainWith(moderator: string): ChainSource {
  const inner = new MockChainSource();
  return {
    getPoolState: async (): Promise<PoolState> => ({
      ...(await inner.getPoolState()),
      moderatorAuthority: moderator,
    }),
    getPendingQueue: () => inner.getPendingQueue(),
    getSubmission: (c) => inner.getSubmission(c),
  };
}

function call(handler: ReturnType<typeof createApp>, method: string, url: string, body?: unknown, headers: Record<string, string> = {}) {
  const chunks: Buffer[] = [];
  if (body !== undefined) chunks.push(Buffer.from(JSON.stringify(body)));
  const req: any = {
    method, url,
    headers: { "content-type": "application/json", ...headers },
    async *[Symbol.asyncIterator]() { for (const c of chunks) yield c; },
  };
  let status = 0;
  const out: Buffer[] = [];
  const res: any = {
    writeHead(s: number) { status = s; },
    end(p?: string) { if (p) out.push(Buffer.from(p)); },
  };
  return handler(req, res).then(() => ({
    status,
    json: () => JSON.parse(Buffer.concat(out).toString("utf8")),
  }));
}

async function setup() {
  const w = wallet();
  const siws = new SiwsVerifier(siwsParams);
  const handler = createApp({
    chain: chainWith(w.b58),
    audit: new AuditLog(join(await mkdtemp(join(tmpdir(), "sixsec-siws-")), "audit.jsonl")),
    auth: siwsAuthenticator(siws),
    siws,
    siwsParams,
    mockData: true,
  });
  return { handler, siws, w };
}

function signWith(priv: KeyObject, message: string): string {
  return base58Encode(new Uint8Array(sign(null, Buffer.from(message, "utf8"), priv)));
}

test("полный честный путь: nonce → подпись → сессия → решение", async () => {
  const { handler, w } = await setup();

  const nonceRes = await call(handler, "POST", "/api/auth/nonce", { pubkey: w.b58 });
  assert.equal(nonceRes.status, 200);
  const { nonce, message } = nonceRes.json();
  assert.ok(message.includes(w.b58), "текст обязан содержать адрес");
  assert.ok(message.includes(siwsParams.domain), "текст обязан содержать домен");

  const verifyRes = await call(handler, "POST", "/api/auth/verify", {
    pubkey: w.b58,
    signature: signWith(w.privateKey, message),
    nonce,
  });
  assert.equal(verifyRes.status, 200);
  const { token } = verifyRes.json();
  assert.ok(token && token.length >= 32, "токен сессии обязан быть длинным");

  const decision = await call(
    handler, "POST", "/api/decisions",
    { claim: "clm1", decision: { kind: "approve", tierId: 2 } },
    { authorization: `Bearer ${token}` },
  );
  assert.equal(decision.status, 202);
  assert.equal(decision.json().signed, false, "backend по-прежнему не подписывает");
});

test("без сессии решение не принимается", async () => {
  const { handler } = await setup();
  const r = await call(handler, "POST", "/api/decisions", { claim: "clm1", decision: { kind: "approve", tierId: 0 } });
  assert.equal(r.status, 401);
});

test("подделанный токен не работает", async () => {
  const { handler } = await setup();
  const r = await call(
    handler, "POST", "/api/decisions",
    { claim: "clm1", decision: { kind: "approve", tierId: 0 } },
    { authorization: "Bearer " + "A".repeat(43) },
  );
  assert.equal(r.status, 401);
});

test("валидная подпись не-модератора сессию не получает", async () => {
  const { handler } = await setup();
  const stranger = wallet();
  const { nonce, message } = (await call(handler, "POST", "/api/auth/nonce", { pubkey: stranger.b58 })).json();
  const r = await call(handler, "POST", "/api/auth/verify", {
    pubkey: stranger.b58,
    signature: signWith(stranger.privateKey, message),
    nonce,
  });
  assert.equal(r.status, 403, "подпись верна, но роль не та");
  assert.equal(r.json().error.code, "NotModeratorAuthority");
});

test("чужой подписью под чужим адресом войти нельзя", async () => {
  const { handler, w } = await setup();
  const attacker = wallet();
  const { nonce, message } = (await call(handler, "POST", "/api/auth/nonce", { pubkey: w.b58 })).json();
  // Атакующий подписывает тот же текст своим ключом и выдаёт себя за модератора.
  const r = await call(handler, "POST", "/api/auth/verify", {
    pubkey: w.b58,
    signature: signWith(attacker.privateKey, message),
    nonce,
  });
  assert.equal(r.status, 401);
  assert.equal(r.json().error.code, "InvalidSignature");
});

test("изменённый текст сообщения не проходит", async () => {
  const { handler, w } = await setup();
  const { nonce, message } = (await call(handler, "POST", "/api/auth/nonce", { pubkey: w.b58 })).json();
  const tampered = message.replace(siwsParams.domain, "evil.example");
  assert.notEqual(tampered, message, "контроль: текст действительно изменён");
  const r = await call(handler, "POST", "/api/auth/verify", {
    pubkey: w.b58,
    signature: signWith(w.privateKey, tampered),
    nonce,
  });
  assert.equal(r.status, 401);
});

test("повторный вход по тому же nonce невозможен", async () => {
  const { handler, w } = await setup();
  const { nonce, message } = (await call(handler, "POST", "/api/auth/nonce", { pubkey: w.b58 })).json();
  const sig = signWith(w.privateKey, message);
  const first = await call(handler, "POST", "/api/auth/verify", { pubkey: w.b58, signature: sig, nonce });
  assert.equal(first.status, 200);
  const second = await call(handler, "POST", "/api/auth/verify", { pubkey: w.b58, signature: sig, nonce });
  assert.equal(second.status, 401);
  assert.equal(second.json().error.code, "NonceReused");
});

test("logout отзывает сессию", async () => {
  const { handler, w } = await setup();
  const { nonce, message } = (await call(handler, "POST", "/api/auth/nonce", { pubkey: w.b58 })).json();
  const { token } = (await call(handler, "POST", "/api/auth/verify", {
    pubkey: w.b58, signature: signWith(w.privateKey, message), nonce,
  })).json();

  assert.equal((await call(handler, "POST", "/api/decisions", { claim: "clm1", decision: { kind: "approve", tierId: 0 } }, { authorization: `Bearer ${token}` })).status, 202);
  assert.equal((await call(handler, "POST", "/api/auth/logout", undefined, { authorization: `Bearer ${token}` })).status, 200);
  assert.equal((await call(handler, "POST", "/api/decisions", { claim: "clm1", decision: { kind: "approve", tierId: 0 } }, { authorization: `Bearer ${token}` })).status, 401);
});

test("мусор в теле auth-запросов не роняет сервер", async () => {
  const { handler } = await setup();
  for (const bad of [{}, { pubkey: 123 }, { pubkey: "0OIl" }]) {
    const r = await call(handler, "POST", "/api/auth/nonce", bad);
    assert.equal(r.status, 400, `ожидался 400 для ${JSON.stringify(bad)}`);
  }
  for (const bad of [{}, { pubkey: "x" }, { pubkey: "x", signature: 5, nonce: "y" }]) {
    const r = await call(handler, "POST", "/api/auth/verify", bad);
    assert.equal(r.status, 400, `ожидался 400 для ${JSON.stringify(bad)}`);
  }
});

test("nonce отдаётся с готовым текстом, который клиент не собирает сам", async () => {
  const { handler, w } = await setup();
  const body = (await call(handler, "POST", "/api/auth/nonce", { pubkey: w.b58 })).json();
  for (const field of ["nonce", "message", "expiresAt"]) {
    assert.ok(body[field], `в ответе обязан быть ${field}`);
  }
  assert.ok(!Number.isNaN(Date.parse(body.expiresAt)));
});
