/**
 * Доменные типы SixSec.
 *
 * Зеркалят ончейн-схему из `programs/sixsec/src/state.rs`. Любое расхождение
 * между этим файлом и state.rs — баг: backend строит инструкции, которые
 * программа отвергнет, либо (что хуже) примет с другим смыслом.
 */

/** Base58 pubkey как строка. Тип-метка, чтобы не путать с произвольной строкой. */
export type Pubkey = string;

/** u64. В JSON сериализуется строкой: Number теряет точность выше 2^53. */
export type U64 = bigint;

export type TaskStatus = "Open" | "Filled" | "Approved" | "Rejected" | "Expired";
export type ClaimStatus = "Active" | "Expired" | "Submitted";
export type ModStatus = "Pending" | "AutoRejected" | "Approved" | "Rejected";

/** ADR-0011: порог, с которого модерация облегчается. Совпадает со state.rs. */
export type ModerationTier = "Full" | "Light";

export const MAX_TRUST = 1000;
export const LIGHT_MODERATION_THRESHOLD = 700;
export const MAX_TIERS = 4;

export interface RewardTier {
  tierId: number;
  tokenAmount: U64;
  tokenMint: Pubkey;
  nftRewardId: Pubkey | null;
}

export interface Task {
  taskId: U64;
  creator: Pubkey;
  briefUri: string;
  tierCount: number;
  tiers: RewardTier[];
  maxClaims: number;
  claimCount: number;
  reservedAmount: U64;
  status: TaskStatus;
  deadline: bigint;
}

export interface WorkerProfile {
  worker: Pubkey;
  trustScore: number;
  approvedCount: number;
  rejectedCount: number;
  autoRejectedCount: number;
}

export interface Submission {
  claim: Pubkey;
  task: Pubkey;
  worker: Pubkey;
  mediaHash: string;
  mediaUri: string;
  consentHash: string;
  submittedAt: bigint;
  moderationStatus: ModStatus;
  awardedTierId: number | null;
  moderator: Pubkey | null;
  rejectionReason: string;
}

export interface PoolState {
  admin: Pubkey;
  /**
   * Единственный адрес, имеющий право одобрять. Читается **с блокчейна**, а не
   * из конфига backend'а: иначе компрометация backend'а давала бы право
   * одобрять выплаты.
   */
  moderatorAuthority: Pubkey;
  skrMint: Pubkey;
  skrPayoutLimit: U64;
}

/** Строка очереди модерации: сабмишен + контекст, нужный человеку для решения. */
export interface QueueItem {
  submission: Submission;
  task: Task;
  worker: WorkerProfile;
  moderationTier: ModerationTier;
}

export function moderationTier(trustScore: number): ModerationTier {
  return trustScore >= LIGHT_MODERATION_THRESHOLD ? "Light" : "Full";
}

/** Сериализация u64 для JSON без потери точности. */
export function u64ToJson(v: U64): string {
  return v.toString();
}
