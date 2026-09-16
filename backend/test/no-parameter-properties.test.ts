import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { glob } from "node:fs/promises";

/**
 * Защита от повторяющейся ошибки.
 *
 * `node --experimental-strip-types` стирает типы, но не генерирует код,
 * поэтому `constructor(private readonly x: T)` падает в рантайме с
 * ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX. Typecheck это НЕ ловит: код валиден
 * по TypeScript. Ошибка уже повторялась трижды, отсюда автоматическая проверка.
 */
const PATTERN = /constructor\s*\(\s*(?:public|private|protected|readonly|override)\s/g;

test("в src/ нет parameter properties", async () => {
  const offenders: string[] = [];
  for await (const file of glob("src/**/*.ts")) {
    const text = await readFile(file, "utf8");
    if (PATTERN.test(text)) offenders.push(file);
  }
  assert.deepEqual(
    offenders,
    [],
    "parameter property несовместим с --experimental-strip-types; объявите поле явно",
  );
});

test("в test/ тоже нет parameter properties", async () => {
  const offenders: string[] = [];
  for await (const file of glob("test/**/*.ts")) {
    const text = await readFile(file, "utf8");
    if (file.endsWith("no-parameter-properties.test.ts")) continue;
    if (PATTERN.test(text)) offenders.push(file);
  }
  assert.deepEqual(offenders, []);
});
