/**
 * Аудит-лог модераторских решений (мастер-промпт: «кто, когда, какой тир,
 * причина отказа»).
 *
 * Append-only JSONL: строки только дописываются, никогда не перезаписываются.
 * Это не замена ончейн-истории — `SubmissionAccount` и так хранит `moderator`,
 * `awarded_tier_id` и `rejection_reason`. Лог нужен для оперативных вопросов,
 * которые ончейн не покрывает: что именно видел модератор до решения и сколько
 * времени оно заняло.
 */

import { appendFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import type { Decision } from "./authorization.ts";
import type { ModerationTier, Pubkey, QueueItem } from "../domain.ts";

export interface AuditEntry {
  /** ISO-8601, UTC. */
  at: string;
  /** Кто принял решение. Обязан совпадать с moderatorAuthority из PoolState. */
  moderator: Pubkey;
  submission: Pubkey;
  task: string;
  worker: Pubkey;
  decision: Decision;
  /** Какой режим проверки применялся — нужен для оценки порога 700. */
  moderationTier: ModerationTier;
  /**
   * Подпись транзакции, если решение уже отправлено. Пустая строка означает
   * «решение принято в UI, транзакция ещё не подписана» — backend решений не
   * подписывает, поэтому это нормальное состояние.
   */
  txSignature: string;
  /** Миллисекунды между открытием карточки и решением. */
  reviewDurationMs: number | null;
}

export class AuditLog {
  // Явное поле, а не parameter property: `node --experimental-strip-types`
  // умеет только стирать типы и не генерирует код присваивания, поэтому
  // `constructor(private readonly ...)` падает с ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX.
  private readonly path: string;

  constructor(path: string) {
    this.path = path;
  }

  async record(entry: Omit<AuditEntry, "at"> & { at?: string }): Promise<AuditEntry> {
    const full: AuditEntry = { at: entry.at ?? new Date().toISOString(), ...entry };
    await mkdir(dirname(this.path), { recursive: true });
    // Одна строка на решение. JSON.stringify без переносов — иначе JSONL
    // перестаёт читаться построчно.
    await appendFile(this.path, JSON.stringify(full) + "\n", "utf8");
    return full;
  }
}

/**
 * Разбор строки JSONL.
 *
 * Возвращает null на битой строке, а не бросает: лог пишется годами, и одна
 * оборванная запись (обрыв диска, kill -9) не должна делать нечитаемым весь
 * аудит-лог.
 */
export function parseAuditLine(line: string): AuditEntry | null {
  const trimmed = line.trim();
  if (trimmed.length === 0) return null;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (typeof parsed !== "object" || parsed === null) return null;
    return parsed as AuditEntry;
  } catch {
    return null;
  }
}

/** Решения конкретного модератора — для выборочной проверки его работы. */
export function decisionsBy(entries: AuditEntry[], moderator: Pubkey): AuditEntry[] {
  return entries.filter((e) => e.moderator === moderator);
}

export function queueItemContext(item: QueueItem): Pick<AuditEntry, "submission" | "task" | "worker" | "moderationTier"> {
  return {
    submission: item.submission.claim,
    task: item.task.taskId.toString(),
    worker: item.submission.worker,
    moderationTier: item.moderationTier,
  };
}
