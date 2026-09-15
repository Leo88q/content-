import { test } from "node:test";
import assert from "node:assert/strict";
import { base58Encode, base58Decode, isValidPubkeyString, Base58Error } from "../src/auth/base58.ts";

test("round-trip на произвольных байтах", () => {
  for (const len of [1, 2, 32, 33, 64]) {
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = (i * 37 + 11) % 256;
    assert.deepEqual(base58Decode(base58Encode(bytes)), bytes, `длина ${len}`);
  }
});

test("ведущие нули не теряются", () => {
  // Классический баг base58-реализаций: ведущие 0x00 обязаны кодироваться '1'.
  const bytes = new Uint8Array([0, 0, 0, 7]);
  const enc = base58Encode(bytes);
  assert.equal(enc.slice(0, 3), "111");
  assert.deepEqual(base58Decode(enc), bytes);
});

test("все нули", () => {
  const bytes = new Uint8Array(32);
  assert.equal(base58Encode(bytes), "1".repeat(32));
  assert.deepEqual(base58Decode("1".repeat(32)), bytes);
});

test("известное значение", () => {
  // "hello world" -> StV1DL6CwTryKyV (проверяемое внешнее значение)
  const enc = base58Encode(new TextEncoder().encode("hello world"));
  assert.equal(enc, "StV1DL6CwTryKyV");
  assert.equal(new TextDecoder().decode(base58Decode(enc)), "hello world");
});

test("пустой вход", () => {
  assert.equal(base58Encode(new Uint8Array(0)), "");
  assert.deepEqual(base58Decode(""), new Uint8Array(0));
});

test("недопустимые символы отклоняются", () => {
  // В алфавите base58 нет 0, O, I, l — их часто путают.
  for (const bad of ["0", "O", "I", "l", "abc0", "hello world", "abc-"]) {
    assert.throws(() => base58Decode(bad), Base58Error, `ожидался отказ для ${JSON.stringify(bad)}`);
  }
});

test("проверка формата адреса", () => {
  const real = "8SrNQieUhEvgPBi1m4Jq6mzLgCo3V2cNGFutRhiUs3U2";
  assert.equal(isValidPubkeyString(real), true);
  assert.equal(isValidPubkeyString(""), false);
  assert.equal(isValidPubkeyString("abc"), false);
  assert.equal(isValidPubkeyString("0".repeat(40)), false);
  assert.equal(isValidPubkeyString("x".repeat(40)), false);
});

test("round-trip на реальном фикстурном адресе", () => {
  const addr = "SKRbvo6Gf7GondiT3BbTfuRDPqLWei4j2Qy2NPGZhW3";
  const dec = base58Decode(addr);
  assert.equal(dec.length, 32, "адрес обязан быть 32 байта");
  assert.equal(base58Encode(dec), addr);
});
