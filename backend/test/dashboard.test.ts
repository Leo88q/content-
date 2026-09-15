import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { createApp, devAuthenticator } from "../src/api/server.ts";
import { MockChainSource, MODERATOR } from "../src/chain/mock.ts";
import { AuditLog } from "../src/moderation/audit.ts";

/**
 * Панель модерации отдаётся одним self-contained файлом: ни CDN, ни шрифтов
 * извне, ни сторонних скриптов. Это не вкусовщина, а граница доверия —
 * страница держит сессионный токен модератора и подписывает challenge ключом.
 * Любой внешний ресурс был бы каналом его утечки, поэтому проверяем явно.
 */

const HTML_PATH = join(import.meta.dirname, "..", "src", "api", "dashboard.html");

const handler = createApp({
  chain: new MockChainSource(),
  audit: new AuditLog(join(await mkdtemp(join(tmpdir(), "sixsec-dash-")), "audit.jsonl")),
  auth: devAuthenticator,
  mockData: true,
});

function call(method: string, url: string) {
  const req: any = { method, url, headers: {}, async *[Symbol.asyncIterator]() {} };
  let status = 0;
  let ctype = "";
  const out: Buffer[] = [];
  const res: any = {
    writeHead(s: number, h: Record<string, string> = {}) {
      status = s;
      ctype = h["content-type"] ?? "";
    },
    end(payload?: string | Buffer) { if (payload) out.push(Buffer.from(payload as string)); },
  };
  return handler(req, res).then(() => ({
    status,
    ctype,
    text: Buffer.concat(out).toString("utf8"),
    json: () => JSON.parse(Buffer.concat(out).toString("utf8")),
  }));
}

test("GET / отдаёт HTML-панель", async () => {
  const r = await call("GET", "/");
  assert.equal(r.status, 200);
  assert.match(r.ctype, /text\/html/);
  assert.match(r.text, /<!doctype html>/i);
  assert.match(r.text, /SixSec/);
});

test("в панели нет внешних скриптов, стилей и шрифтов", async () => {
  const html = await readFile(HTML_PATH, "utf8");
  assert.doesNotMatch(html, /<script[^>]+\bsrc\s*=/i, "внешний <script src>");
  assert.doesNotMatch(html, /<link\b/i, "внешний <link>");
  assert.doesNotMatch(html, /@import/i, "@import в CSS");
  assert.doesNotMatch(html, /url\(\s*["']?https?:/i, "внешний url() в CSS");
  assert.doesNotMatch(html, /fonts\.(googleapis|gstatic)|cdn\.|unpkg|jsdelivr/i, "CDN");
});

test("каждый url() — либо inline data-URI, либо внутренний фрагмент", async () => {
  const html = await readFile(HTML_PATH, "utf8");
  const urls = [...html.matchAll(/url\(\s*["']?([^"')]+)/g)].map((m) => m[1] ?? "");
  assert.ok(urls.length >= 1, "зерно фона должно быть data-URI");
  // Внутри data-URI лежит filter='url(%23n)' — это ссылка на фильтр того же
  // SVG, %23 это "#". Обе формы внутренние, внешних загрузок не делают.
  for (const u of urls) {
    const internal = /^data:image\/svg\+xml/.test(u) || u.startsWith("#") || u.startsWith("%23");
    assert.ok(internal, `внешний url(): ${u.slice(0, 60)}`);
  }
  assert.ok(urls.some((u) => u.startsWith("data:image/svg+xml")), "нужен хотя бы один data-URI");
});

test("статические src/href ведут внутрь страницы или на шаблонное выражение", async () => {
  const html = await readFile(HTML_PATH, "utf8");
  // Из шаблонов в <script> вырезаем только атрибуты вне шаблонных строк
  // проверить нельзя, поэтому разрешаем три законных варианта: "#", путь от
  // корня и interpolation "${...}" (её безопасность даёт safeUrl + esc).
  const attrs = [...html.matchAll(/\b(?:src|href)\s*=\s*"([^"]*)"/g)].map((m) => m[1] ?? "");
  assert.ok(attrs.length >= 2, "атрибуты должны найтись, иначе проверка пуста");
  for (const a of attrs) {
    // "#" и "#якорь" — внутренние фрагменты, "/…" — путь по этому же
    // серверу, "${…}" — подстановка, её безопасность дают safeUrl и esc.
    const ok = a.startsWith("#") || a.startsWith("/") || a.startsWith("${");
    assert.ok(ok, `подозрительный адрес в разметке: ${a}`);
  }
});

test("в панели нет динамического выполнения кода", async () => {
  const html = await readFile(HTML_PATH, "utf8");
  for (const bad of ["eval(", "new Function", "document.write", "setTimeout(\"", "Function(\""]) {
    assert.ok(!html.includes(bad), `найдено ${bad}`);
  }
});

test("адреса в href проходят через safeUrl, а текст — через esc", async () => {
  const html = await readFile(HTML_PATH, "utf8");
  // Каждый href с подстановкой обязан быть обёрнут в safeUrl: одно только
  // экранирование не спасает от javascript: в атрибуте href.
  const dynamic = [...html.matchAll(/href\s*=\s*"(\$\{[^"]*\})"/g)].map((m) => m[1] ?? "");
  assert.ok(dynamic.length >= 1, "динамические href должны быть");
  for (const d of dynamic) {
    assert.match(d, /safeUrl\(/, `href без safeUrl: ${d}`);
  }
  assert.match(html, /const esc = /, "должен быть хелпер экранирования");
  assert.match(html, /function safeUrl/, "должен быть хелпер safeUrl");
  assert.match(html, /protocol === "http:"/, "safeUrl обязан разрешать только http(s)");
});

test("/api/queue отдаёт состояние пула для панели", async () => {
  const r = await call("GET", "/api/queue");
  assert.equal(r.status, 200);
  const b = r.json();
  assert.ok(b.pool, "поле pool должно присутствовать");
  assert.equal(typeof b.pool.admin, "string");
  assert.equal(typeof b.pool.skrMint, "string");
  // U64 сериализуется строкой, иначе на больших значениях теряется точность.
  assert.equal(typeof b.pool.skrPayoutLimit, "string");
  // 50_000_000_000n из фикстуры: убеждаемся, что jsonReplacer действительно
  // превратил bigint в строку, а не потерял значение.
  assert.equal(b.pool.skrPayoutLimit, "50000000000");
  assert.equal(b.moderatorAuthority, MODERATOR);
  assert.ok(b.items.length > 0, "без элементов остальные проверки пусты");
});

test("счётчики очереди согласованы со списком", async () => {
  const b = (await call("GET", "/api/queue")).json();
  assert.equal(b.counts.total, b.items.length);
  assert.equal(
    b.counts.full,
    b.items.filter((i: { moderationTier: string }) => i.moderationTier === "Full").length,
  );
  assert.equal(b.counts.total, b.counts.full + b.counts.light);
});
