/**
 * Авторизация и валидация модераторских решений.
 *
 * `moderate` — единственная инструкция, пишущая `trust_score`, и единственный
 * путь к выплате. Поэтому здесь дублируются проверки ончейн-программы: не
 * потому что backend им не доверяет, а чтобы неверное решение отсекалось до
 * траты транзакции и чтобы расхождение между двумя реализациями было видно.
 *
 * **Backend не подписывает решения.** Он возвращает полезную нагрузку для
 * подписи кошельком модератора. Скомпрометированный backend поэтому не может
 * одобрить ни одного сабмишена — подпись всё равно нужна от
 * `moderatorAuthority`.
 */

import type { Pubkey, QueueItem } from "../domain.ts";

export type Decision =
  | { kind: "approve"; tierId: number }
  | { kind: "reject"; reason: string };

export type AuthorizationError =
  | { code: "NotModeratorAuthority"; signer: Pubkey; expected: Pubkey }
  | { code: "AlreadyDecided"; status: string }
  | { code: "InvalidTierId"; tierId: number; tierCount: number }
  | { code: "EmptyRejectionReason" }
  | { code: "RejectionReasonTooLong"; length: number; max: number };

export type AuthorizationResult =
  | { ok: true }
  | { ok: false; error: AuthorizationError };

/** Ограничение ончейн-поля `rejection_reason` (String ограниченной длины). */
export const MAX_REJECTION_REASON_LEN = 280;

/**
 * Проверка права подписанта принимать решения.
 *
 * `expectedAuthority` обязан приходить из `PoolState`, прочитанного с
 * блокчейна. Подставлять сюда значение из env или конфига нельзя: это
 * превращает безопасность backend'а в безопасность его конфига.
 */
export function assertCanModerate(
  signer: Pubkey,
  expectedAuthority: Pubkey,
): AuthorizationResult {
  // Сравнение строк чувствительно к регистру: base58-адреса регистрозависимы,
  // и нормализация здесь создала бы способ обойти проверку похожим адресом.
  if (signer !== expectedAuthority) {
    return {
      ok: false,
      error: { code: "NotModeratorAuthority", signer, expected: expectedAuthority },
    };
  }
  return { ok: true };
}

/** Валидация самого решения, независимо от того, кто его принимает. */
export function validateDecision(
  decision: Decision,
  item: QueueItem,
): AuthorizationResult {
  if (item.submission.moderationStatus !== "Pending") {
    return {
      ok: false,
      error: {
        code: "AlreadyDecided",
        status: item.submission.moderationStatus,
      },
    };
  }

  if (decision.kind === "approve") {
    // Тир обязан существовать в задании: approve с несуществующим тиром —
    // это либо ошибка UI, либо попытка выплатить несуществующую награду.
    if (decision.tierId < 0 || decision.tierId >= item.task.tierCount) {
      return {
        ok: false,
        error: {
          code: "InvalidTierId",
          tierId: decision.tierId,
          tierCount: item.task.tierCount,
        },
      };
    }
    return { ok: true };
  }

  const reason = decision.reason.trim();
  if (reason.length === 0) {
    // Отказ без причины обесценивает всю обратную связь для воркера: в модели
    // конкурса (ADR-0003) отклонённые работали впустую и должны понимать, что
    // именно не так, иначе репутация площадки падает.
    return { ok: false, error: { code: "EmptyRejectionReason" } };
  }
  if (reason.length > MAX_REJECTION_REASON_LEN) {
    return {
      ok: false,
      error: {
        code: "RejectionReasonTooLong",
        length: reason.length,
        max: MAX_REJECTION_REASON_LEN,
      },
    };
  }
  return { ok: true };
}

/**
 * Полная проверка: право + корректность решения.
 *
 * Порядок намеренный: сначала право, потом решение. Иначе неавторизованный
 * вызывающий мог бы по кодам ошибок изучать содержимое очереди.
 */
export function authorizeDecision(
  signer: Pubkey,
  authority: Pubkey,
  decision: Decision,
  item: QueueItem,
): AuthorizationResult {
  const canModerate = assertCanModerate(signer, authority);
  if (!canModerate.ok) return canModerate;
  return validateDecision(decision, item);
}
