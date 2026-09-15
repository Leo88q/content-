import type { ChainSource } from "./source.ts";
import {
  MAX_TRUST,
  LIGHT_MODERATION_THRESHOLD,
  moderationTier,
  type PoolState,
  type Pubkey,
  type QueueItem,
  type Submission,
  type Task,
  type WorkerProfile,
} from "../domain.ts";

/** Заглушки адресов. Не являются реальными ключами. */
export const MODERATOR = "Mod11111111111111111111111111111111111111111";
export const ADMIN = "Adm1111111111111111111111111111111111111111111";
export const GAME_MINT = "GmTk111111111111111111111111111111111111111111";
export const SKR_MINT = "SKRbvo6Gf7GondiT3BbTfuRDPqLWei4j2Qy2NPGZhW3";

function task(id: number, tierCount: number): Task {
  return {
    taskId: BigInt(id),
    creator: ADMIN,
    briefUri: `https://arweave.net/brief-${id}`,
    tierCount,
    tiers: Array.from({ length: tierCount }, (_, i) => ({
      tierId: i,
      tokenAmount: BigInt((i + 1) * 1_000_000),
      tokenMint: GAME_MINT,
      nftRewardId: null,
    })),
    maxClaims: 3,
    claimCount: 1,
    reservedAmount: BigInt(tierCount * 1_000_000),
    status: "Open",
    deadline: BigInt(Math.floor(Date.now() / 1000) + 86_400),
  };
}

function worker(addr: Pubkey, trust: number): WorkerProfile {
  return {
    worker: addr,
    trustScore: trust,
    approvedCount: Math.floor(trust / 15),
    rejectedCount: 0,
    autoRejectedCount: 0,
  };
}

function submission(claim: string, taskId: number, w: Pubkey): Submission {
  return {
    claim,
    task: `Task${taskId}`,
    worker: w,
    mediaHash: "a".repeat(64),
    mediaUri: `https://arweave.net/clip-${claim}`,
    consentHash: "b".repeat(64),
    submittedAt: BigInt(Math.floor(Date.now() / 1000) - 600),
    moderationStatus: "Pending",
    awardedTierId: null,
    moderator: null,
    rejectionReason: "",
  };
}

/**
 * Фикстурный набор, покрывающий оба режима модерации: новичок (Full) и
 * работник выше порога 700 (Light). Без обоих кейсов невозможно проверить,
 * что порог вообще на что-то влияет.
 */
export function fixtureQueue(): QueueItem[] {
  const newcomer = "Wrk1111111111111111111111111111111111111111111";
  const veteran = "Wrk2222222222222222222222222222222222222222222";
  const rows: Array<[string, number, Pubkey, number]> = [
    ["clm1", 1, newcomer, 0],
    ["clm2", 1, veteran, MAX_TRUST],
    ["clm3", 2, newcomer, 300],
    ["clm4", 2, veteran, LIGHT_MODERATION_THRESHOLD],
  ];
  return rows.map(([claim, taskId, w, trust]) => {
    const prof = worker(w, trust);
    return {
      submission: submission(claim, taskId, w),
      task: task(taskId, 3),
      worker: prof,
      moderationTier: moderationTier(prof.trustScore),
    };
  });
}

export class MockChainSource implements ChainSource {
  private readonly queue = fixtureQueue();

  async getPoolState(): Promise<PoolState> {
    return {
      admin: ADMIN,
      moderatorAuthority: MODERATOR,
      skrMint: SKR_MINT,
      skrPayoutLimit: 50_000_000_000n,
    };
  }

  async getPendingQueue(): Promise<QueueItem[]> {
    return this.queue.filter((i) => i.submission.moderationStatus === "Pending");
  }

  async getSubmission(claim: Pubkey): Promise<Submission | null> {
    return this.queue.find((i) => i.submission.claim === claim)?.submission ?? null;
  }
}
