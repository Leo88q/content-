import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp, DEFAULT_SIWS_PARAMS } from "../src/api/server.ts";
import { SiwsVerifier } from "../src/auth/siws.ts";
import { MockChainSource } from "../src/chain/mock.ts";
import { AuditLog } from "../src/moderation/audit.ts";

function call(handler: ReturnType<typeof createApp>, method: string, url: string) {
  const req: any = { method, url, headers: {}, async *[Symbol.asyncIterator]() {} };
  let status = 0;
  const out: Buffer[] = [];
  const res: any = { writeHead(s: number) { status = s; }, end(p?: string) { if (p) out.push(Buffer.from(p)); } };
  return handler(req, res).then(() => ({ status, body: Buffer.concat(out).toString("utf8") }));
}

async function app(mockData: boolean) {
  const siws = new SiwsVerifier(DEFAULT_SIWS_PARAMS);
  return createApp({
    chain: new MockChainSource(),
    audit: new AuditLog(join(await mkdtemp(join(tmpdir(), "sixsec-guard-")), "audit.jsonl")),
    auth: { signerOf: () => null },
    siws,
    // demoKey присутствует, но mockData выключен — именно это сочетание и проверяется.
    demoKey: { pubkey: "8SrNQieUhEvgPBi1m4Jq6mzLgCo3V2cNGFutRhiUs3U2", pkcs8Base64: "ZmFrZQ==" },
    mockData,
  });
}

test("вне mock-режима демо-ключ не отдаётся", async () => {
  const r = await call(await app(false), "GET", "/api/dev/demo-key");
  assert.equal(r.status, 404);
  assert.ok(!r.body.includes("pkcs8"), "приватный ключ не должен попасть в ответ");
});

test("в mock-режиме демо-ключ доступен", async () => {
  const r = await call(await app(true), "GET", "/api/dev/demo-key");
  assert.equal(r.status, 200);
  assert.ok(r.body.includes("pkcs8Base64"));
});

test("health честно сообщает о режиме данных", async () => {
  assert.equal((await call(await app(true), "GET", "/api/health")).body.includes('"mockData":true'), true);
  assert.equal((await call(await app(false), "GET", "/api/health")).body.includes('"mockData":false'), true);
});
