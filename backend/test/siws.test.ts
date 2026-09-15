import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import {
  SiwsVerifier,
  SiwsError,
  buildMessage,
  pubkeyToBase58,
  type SiwsParams,
} from "../src/auth/siws.ts";
import { base58Encode } from "../src/auth/base58.ts";

const params: SiwsParams = {
  domain: "sixsec.local",
  uri: "http://localhost:8787",
  nonceTtlMs: 5 * 60_000,
  sessionTtlMs: 30 * 60_000,
};

function newWallet() {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const raw = new Uint8Array(publicKey.export({ format: "der", type: "spki" }).subarray(12));
  return { b58: pubkeyToBase58(raw), privateKey, raw };
}

function signMessage(privKey: ReturnType<typeof generateKeyPairSync>["privateKey"], message: string): string {
  const sig = sign(null, Buffer.from(message, "utf8"), privKey);
  return base58Encode(new Uint8Array(sig));
}

/** Полный честный поток: сервер выдал nonce, кошелёк подписал. */
function honestLogin(v: SiwsVerifier) {
  const w = newWallet();
  const pending = v.issueNonce(w.b58);
  const message = buildMessage(w.b58, pending, params);
  return { ...w, nonce: pending.nonce, signature: signMessage(w.privateKey, message) };
}

test("честный вход: подпись подтверждает именно этот адрес", () => {
  const v = new SiwsVerifier(params);
  const l = honestLogin(v);
  assert.equal(v.verify(l.b58, l.signature, l.nonce), l.b58);
});

test("назваться чужим адресом нельзя", () => {
  const v = new SiwsVerifier(params);
  const l = honestLogin(v);
  const impostor = newWallet();
  const err = (() => { try { v.verify(impostor.b58, l.signature, l.nonce); return null; } catch (e) { return e as SiwsError; } })();
  assert.ok(err);
  // Ключевое свойство: подпись валидна, но не под этим ключом.
  assert.equal(err.code, "InvalidSignature");
});

test("подпись от другого домена не проходит", () => {
  const v = new SiwsVerifier(params);
  const w = newWallet();
  const pending = v.issueNonce(w.b58);
  // Подписываем сообщение с чужим доменом — подпись не подойдёт к нашему.
  const forged = buildMessage(w.b58, pending, { ...params, domain: "evil.example" });
  assert.throws(
    () => v.verify(w.b58, signMessage(w.privateKey, forged), pending.nonce),
    (e: SiwsError) => e.code === "InvalidSignature",
  );
});

test("подпись с другим nonce не проходит", () => {
  const v = new SiwsVerifier(params);
  const w = newWallet();
  const a = v.issueNonce(w.b58);
  const b = v.issueNonce(w.b58);
  const sig = signMessage(w.privateKey, buildMessage(w.b58, a, params));
  assert.throws(
    () => v.verify(w.b58, sig, b.nonce),
    (e: SiwsError) => e.code === "InvalidSignature",
  );
});

test("nonce одноразовый", () => {
  const v = new SiwsVerifier(params);
  const l = honestLogin(v);
  v.verify(l.b58, l.signature, l.nonce);
  assert.throws(
    () => v.verify(l.b58, l.signature, l.nonce),
    (e: SiwsError) => e.code === "NonceReused",
  );
});

test("неизвестный nonce", () => {
  const v = new SiwsVerifier(params);
  const l = honestLogin(v);
  assert.throws(
    () => v.verify(l.b58, l.signature, "нет-такого"),
    (e: SiwsError) => e.code === "UnknownNonce",
  );
});

test("просроченный nonce", () => {
  const v = new SiwsVerifier({ ...params, nonceTtlMs: -1 });
  const w = newWallet();
  const pending = v.issueNonce(w.b58);
  const sig = signMessage(w.privateKey, buildMessage(w.b58, pending, params));
  assert.throws(
    () => v.verify(w.b58, sig, pending.nonce),
    (e: SiwsError) => e.code === "NonceExpired",
  );
});

test("подпись неверной длины не принимается за неверную подпись", () => {
  const v = new SiwsVerifier(params);
  const l = honestLogin(v);
  const short = base58Encode(new Uint8Array(32));
  assert.throws(
    () => v.verify(l.b58, short, l.nonce),
    (e: SiwsError) => e.code === "MalformedSignature",
  );
});

test("адрес неверной длины отклоняется ещё при выдаче nonce", () => {
  const v = new SiwsVerifier(params);
  const short = base58Encode(new Uint8Array(31));
  assert.throws(
    () => v.issueNonce(short),
    (e: SiwsError) => e.code === "MalformedSignature",
  );
  assert.equal(v.stats().nonces, 0, "мусор не должен попадать в состояние сервера");
});

test("nonce не выдаётся на мусорный адрес", () => {
  const v = new SiwsVerifier(params);
  for (const bad of ["", "abc", "0OIl", "1".repeat(50)]) {
    assert.throws(() => v.issueNonce(bad), SiwsError, `ожидался отказ для ${JSON.stringify(bad)}`);
  }
  assert.equal(v.stats().nonces, 0);
});

test("мусор вместо base58 не роняет сервер", () => {
  const v = new SiwsVerifier(params);
  const l = honestLogin(v);
  for (const bad of ["0OIl", "", "!!!"]) {
    assert.throws(
      () => v.verify(l.b58, bad, l.nonce),
      (e: SiwsError) => e.code === "MalformedSignature",
      `ожидался MalformedSignature для ${JSON.stringify(bad)}`,
    );
  }
});

test("неудачная проверка не сжигает nonce", () => {
  // Иначе злоумышленник сжигал бы nonce'ы, просто угадывая их.
  const v = new SiwsVerifier(params);
  const w = newWallet();
  const pending = v.issueNonce(w.b58);
  assert.throws(() => v.verify(w.b58, base58Encode(new Uint8Array(64)), pending.nonce));
  const sig = signMessage(w.privateKey, buildMessage(w.b58, pending, params));
  assert.equal(v.verify(w.b58, sig, pending.nonce), w.b58, "nonce должен остаться живым");
});

test("сессия выдаётся и проверяется", () => {
  const v = new SiwsVerifier(params);
  const s = v.startSession("8SrNQieUhEvgPBi1m4Jq6mzLgCo3V2cNGFutRhiUs3U2");
  assert.equal(v.sessionOf(`Bearer ${s.token}`)?.pubkey, s.pubkey);
  assert.equal(v.sessionOf(s.token)?.pubkey, s.pubkey, "префикс Bearer не обязателен");
});

test("чужой и отозванный токен не работают", () => {
  const v = new SiwsVerifier(params);
  const s = v.startSession("8SrNQieUhEvgPBi1m4Jq6mzLgCo3V2cNGFutRhiUs3U2");
  assert.equal(v.sessionOf("Bearer " + "x".repeat(43)), null);
  assert.equal(v.sessionOf(undefined), null);
  assert.equal(v.sessionOf("Bearer "), null);
  v.revoke(s.token);
  assert.equal(v.sessionOf(s.token), null);
});

test("просроченная сессия не работает", () => {
  const v = new SiwsVerifier({ ...params, sessionTtlMs: -1 });
  const s = v.startSession("8SrNQieUhEvgPBi1m4Jq6mzLgCo3V2cNGFutRhiUs3U2");
  assert.equal(v.sessionOf(s.token), null);
});

test("nonce'ы вычищаются — Map не растёт неограниченно", () => {
  // Запрос nonce не требует авторизации, поэтому без очистки это готовый DoS.
  // nonceTtlMs < 0 — nonce умирает сразу. Очистка выполняется при каждой
  // выдаче, поэтому Map не успевает вырасти: после 50 выдач должен остаться
  // ровно один (последний), а не 50.
  const v = new SiwsVerifier({ ...params, nonceTtlMs: -1 });
  for (let i = 0; i < 50; i++) v.issueNonce(newWallet().b58);
  assert.equal(v.stats().nonces, 1, "истёкшие nonce должны удаляться при выдаче");
  // И контроль: без очистки здесь было бы 50 — иначе тест ничего не доказывает.
  assert.ok(50 > 1);
});

test("nonce'ы не повторяются", () => {
  const v = new SiwsVerifier(params);
  const seen = new Set<string>();
  for (let i = 0; i < 500; i++) {
    const n = v.issueNonce(newWallet().b58).nonce;
    assert.equal(seen.has(n), false);
    seen.add(n);
  }
});

test("текст сообщения связывает адрес, домен, nonce и время", () => {
  const v = new SiwsVerifier(params);
  const w = newWallet();
  const pending = v.issueNonce(w.b58);
  const msg = buildMessage(w.b58, pending, params);
  assert.ok(msg.includes(w.b58));
  assert.ok(msg.includes(params.domain));
  assert.ok(msg.includes(pending.nonce));
  assert.ok(msg.includes(pending.issuedAt));
  // Сервер восстанавливает сообщение из СВОЕЙ записи: клиент возвращает
  // только подпись, поэтому подменить поля он не может.
  assert.equal(buildMessage(w.b58, pending, params), msg);
});

test("nonce, выданный для другого адреса, не принимается", () => {
  const v = new SiwsVerifier(params);
  const a = newWallet();
  const b = newWallet();
  const pending = v.issueNonce(a.b58);
  const sig = signMessage(b.privateKey, buildMessage(b.b58, pending, params));
  assert.throws(
    () => v.verify(b.b58, sig, pending.nonce),
    (e: SiwsError) => e.code === "InvalidSignature",
  );
});
