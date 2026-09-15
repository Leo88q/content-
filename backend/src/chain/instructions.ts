/**
 * Описание ончейн-инструкций, которое backend отдаёт кошельку на подпись.
 *
 * Спецификация живёт в `moderate.spec.json` и сверяется с настоящим IDL в CI.
 * До этого backend собирал описание руками в обработчике и разошёлся с программой
 * в трёх местах: `tier_id` и `reason` в программе — `Option`, а backend слал `0`
 * и `""`; из пяти аккаунтов был указан один. Кошелёк с таким описанием не собрал
 * бы валидную транзакцию.
 */

import specJson from "./moderate.spec.json" with { type: "json" };
import type { Decision } from "../moderation/authorization.ts";
import type { Pubkey, QueueItem } from "../domain.ts";

export interface AccountSpec {
  name: string;
  isMut: boolean;
  isSigner: boolean;
}

export interface InstructionSpec {
  instruction: string;
  args: Array<{ name: string; type: unknown }>;
  accounts: AccountSpec[];
}

export const MODERATE_SPEC = specJson as InstructionSpec;

export const PROGRAM_ID = "SixSec1111111111111111111111111111111111111";

export interface ResolvedAccount {
  name: string;
  pubkey: Pubkey | null;
  isMut: boolean;
  isSigner: boolean;
}

export interface InstructionPayload {
  program: string;
  name: string;
  /** Аргументы в порядке, объявленном в программе. None кодируется как null. */
  args: Record<string, unknown>;
  /** Полный список аккаунтов в порядке, требуемом программой. */
  accounts: ResolvedAccount[];
  signerRequired: Pubkey;
  /**
   * Адреса, которые backend не может вычислить без доступа к чейну.
   * Кошелёк обязан их дозаполнить; пустой список означает, что описание полное.
   */
  unresolved: string[];
}

/**
 * Собирает описание инструкции `moderate` для подписи.
 *
 * `Option`-аргументы кодируются как `null`, а не как `0`/`""`: программа
 * различает «тир не выбран» и «выбран тир 0», и подмена меняет смысл выплаты.
 */
export function buildModeratePayload(
  decision: Decision,
  item: QueueItem,
  moderatorAuthority: Pubkey,
): InstructionPayload {
  // Ключи — snake_case, как имена аккаунтов в IDL.
  const known: Record<string, Pubkey> = {
    moderator: moderatorAuthority,
    task: item.submission.task,
    submission: item.submission.claim,
    // worker_profile — PDA ["profile", worker]; вычисляется детерминированно,
    // но без знания bump'а backend его не подставляет.
  };

  const accounts: ResolvedAccount[] = MODERATE_SPEC.accounts.map((a) => ({
    name: a.name,
    pubkey: known[a.name] ?? null,
    isMut: a.isMut,
    isSigner: a.isSigner,
  }));

  return {
    program: PROGRAM_ID,
    name: MODERATE_SPEC.instruction,
    args: {
      approve: decision.kind === "approve",
      tier_id: decision.kind === "approve" ? decision.tierId : null,
      reason: decision.kind === "reject" ? decision.reason : null,
    },
    accounts,
    signerRequired: moderatorAuthority,
    unresolved: accounts.filter((a) => a.pubkey === null).map((a) => a.name),
  };
}
