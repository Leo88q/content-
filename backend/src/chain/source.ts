/**
 * Абстракция источника ончейн-данных.
 *
 * Программа SixSec сейчас не задеплоена никуда, включая devnet. Backend тем не
 * менее строится и проверяется уже сейчас, поэтому чтение отделено интерфейсом:
 * сегодня работает `MockChainSource` на фикстурах, после деплоя добавляется
 * реализация поверх `@solana/kit`, и остальной код не меняется.
 *
 * Граница честности: пока активна mock-реализация, дашборд показывает
 * **выдуманные** сабмишены. Это годится для отработки UX и проверок
 * авторизации и не годится для реальных выплат.
 */

import type { PoolState, Pubkey, QueueItem, Submission } from "../domain.ts";

export interface ChainSource {
  /** Состояние пула — источник moderatorAuthority. Обязан читаться с чейна. */
  getPoolState(): Promise<PoolState>;

  /** Очередь на модерацию, отсортированная для показа человеку. */
  getPendingQueue(): Promise<QueueItem[]>;

  /** Один сабмишен для карточки модератора. */
  getSubmission(claim: Pubkey): Promise<Submission | null>;
}

/** Ошибки источника, которые caller обязан различать. */
export type ChainError =
  | { code: "NotFound"; what: string }
  | { code: "Unavailable"; detail: string };
