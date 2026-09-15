/**
 * HTTP-сервер очереди модерации.
 *
 * Только встроенный `node:http`: ни фреймворка, ни сборки. Намеренно —
 * зависимостей меньше, и `node --experimental-strip-types` поднимает сервер без
 * шага компиляции.
 *
 * ⚠️ **Backend не подписывает и не отправляет транзакции.** `POST /api/decisions`
 * проверяет право и корректность, пишет аудит-лог и возвращает описание
 * инструкции, которую подписывает кошелёк модератора. Скомпрометированный
 * backend поэтому не способен одобрить выплату.
 */

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { MockChainSource } from "../chain/mock.ts";
import type { ChainSource } from "../chain/source.ts";
import { AuditLog } from "../moderation/audit.ts";
import { authorizeDecision, type Decision } from "../moderation/authorization.ts";
import type { QueueItem } from "../domain.ts";

/**
 * Кто подписант.
 *
 * ⚠️ DEV-РЕЖИМ: адрес берётся из заголовка `x-moderator` без криптографической
 * проверки. Для прода обязателен SIWS (Sign-In With Solana): подпись сообщения
 * кошельком и сверка восстановленного адреса с `PoolState.moderatorAuthority`.
 * Пока этого нет, сервер годится только для локальной отработки.
 */
export interface Authenticator {
  signerOf(req: IncomingMessage): string | null;
}

export const devAuthenticator: Authenticator = {
  signerOf(req) {
    const h = req.headers["x-moderator"];
    return typeof h === "string" && h.length > 0 ? h : null;
  },
};

export interface AppDeps {
  chain: ChainSource;
  audit: AuditLog;
  auth: Authenticator;
  /** true, пока источник данных — фикстуры. Показывается в UI. */
  mockData: boolean;
}

/**
 * Сериализатор ответа.
 *
 * bigint обязан уходить строкой: `JSON.stringify` на bigint бросает TypeError,
 * а приведение к Number теряет точность выше 2^53. Награда в 10^9 токенов с
 * decimals 6 — это 10^15, ещё безопасно, но u64-резерв или лимит эпохи легко
 * превышают порог, и молчаливая потеря разряда в сумме награды недопустима.
 */
export function jsonReplacer(_key: string, value: unknown): unknown {
  return typeof value === "bigint" ? value.toString() : value;
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body, jsonReplacer);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(payload);
}

async function readBody(req: IncomingMessage, limit = 64 * 1024): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buf = chunk as Buffer;
    size += buf.length;
    if (size > limit) throw new Error("payload too large");
    chunks.push(buf);
  }
  if (chunks.length === 0) return null;
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

/** Разбор решения из тела запроса. Всё непроверенное — неизвестный тип. */
export function parseDecision(raw: unknown): Decision | null {
  if (typeof raw !== "object" || raw === null) return null;
  const o = raw as Record<string, unknown>;
  if (o.kind === "approve") {
    if (typeof o.tierId !== "number" || !Number.isInteger(o.tierId)) return null;
    return { kind: "approve", tierId: o.tierId };
  }
  if (o.kind === "reject") {
    if (typeof o.reason !== "string") return null;
    return { kind: "reject", reason: o.reason };
  }
  return null;
}

export function createApp(deps: AppDeps) {
  const { chain, audit, auth } = deps;

  return async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? "/", "http://localhost");
    try {
      if (url.pathname === "/api/health") {
        sendJson(res, 200, { ok: true, mockData: deps.mockData });
        return;
      }

      if (url.pathname === "/api/queue" && req.method === "GET") {
        const [items, pool] = await Promise.all([chain.getPendingQueue(), chain.getPoolState()]);
        sendJson(res, 200, {
          mockData: deps.mockData,
          moderatorAuthority: pool.moderatorAuthority,
          counts: {
            total: items.length,
            full: items.filter((i) => i.moderationTier === "Full").length,
            light: items.filter((i) => i.moderationTier === "Light").length,
          },
          items,
        });
        return;
      }

      if (url.pathname === "/api/decisions" && req.method === "POST") {
        const signer = auth.signerOf(req);
        if (signer === null) {
          sendJson(res, 401, { error: { code: "Unauthenticated" } });
          return;
        }
        const body = (await readBody(req)) as Record<string, unknown> | null;
        const claim = typeof body?.claim === "string" ? body.claim : null;
        const decision = parseDecision(body?.decision);
        if (claim === null || decision === null) {
          sendJson(res, 400, { error: { code: "MalformedRequest" } });
          return;
        }

        const [pool, items] = await Promise.all([chain.getPoolState(), chain.getPendingQueue()]);
        const item: QueueItem | undefined = items.find((i) => i.submission.claim === claim);
        if (item === undefined) {
          sendJson(res, 404, { error: { code: "NotFound", what: claim } });
          return;
        }

        const authz = authorizeDecision(signer, pool.moderatorAuthority, decision, item);
        if (!authz.ok) {
          // 403 для чужого кошелька, 422 для некорректного решения: это разные
          // ситуации и в UI, и в аудите.
          sendJson(res, authz.error.code === "NotModeratorAuthority" ? 403 : 422, {
            error: authz.error,
          });
          return;
        }

        const entry = await audit.record({
          moderator: signer,
          submission: item.submission.claim,
          task: item.task.taskId.toString(),
          worker: item.submission.worker,
          decision,
          moderationTier: item.moderationTier,
          txSignature: "",
          reviewDurationMs:
            typeof body?.reviewDurationMs === "number" ? body.reviewDurationMs : null,
        });

        sendJson(res, 202, {
          accepted: true,
          auditAt: entry.at,
          signed: false,
          // Инструкция к подписи кошельком. Backend её не подписывает.
          instruction: {
            program: "SixSec1111111111111111111111111111111111111",
            name: "moderate",
            args: {
              approve: decision.kind === "approve",
              tierId: decision.kind === "approve" ? decision.tierId : 0,
              reason: decision.kind === "reject" ? decision.reason : "",
            },
            accounts: { submission: item.submission.claim },
            signerRequired: pool.moderatorAuthority,
          },
          mockData: deps.mockData,
        });
        return;
      }

      if (url.pathname === "/" || url.pathname === "/index.html") {
        const html = await readFile(join(import.meta.dirname, "dashboard.html"));
        res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
        res.end(html);
        return;
      }

      sendJson(res, 404, { error: { code: "NotFound", what: url.pathname } });
    } catch (err) {
      sendJson(res, 500, {
        error: { code: "Internal", detail: err instanceof Error ? err.message : String(err) },
      });
    }
  };
}

export function startServer(deps: AppDeps, port: number): ReturnType<typeof createServer> {
  const handler = createApp(deps);
  const server = createServer((req, res) => {
    void handler(req, res);
  });
  // 0.0.0.0, а не 127.0.0.1: сервер должен быть виден снаружи контейнера.
  server.listen(port, "0.0.0.0", () => {
    console.log(`SixSec moderation API: http://0.0.0.0:${port}`);
    if (deps.mockData) {
      console.log("⚠️  Источник данных — ФИКСТУРЫ. Реальных выплат здесь нет.");
    }
  });
  return server;
}

const isMain = process.argv[1] !== undefined && import.meta.url.endsWith(process.argv[1].split("/").pop()!);
if (isMain) {
  const auditPath = process.env.SIXSEC_AUDIT_LOG ?? join(process.cwd(), "data", "audit.jsonl");
  startServer(
    { chain: new MockChainSource(), audit: new AuditLog(auditPath), auth: devAuthenticator, mockData: true },
    Number(process.env.PORT ?? 8787),
  );
}
