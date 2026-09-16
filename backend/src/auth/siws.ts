/**
 * Sign-In With Solana для модератора.
 *
 * Заменяет dev-заглушку, где адрес брался из заголовка `x-moderator` без
 * криптопроверки. Здесь адрес **восстанавливается из подписи**, поэтому
 * назвать себя чужим адресом нельзя.
 *
 * Используется ed25519 из `node:crypto` — тот же алгоритм, что у Solana, —
 * а не `@solana/kit`: для проверки подписи весь SDK не нужен.
 */

import {
  createPublicKey,
  randomBytes,
  randomUUID,
  timingSafeEqual,
  verify as edVerify,
} from "node:crypto";
import {
  base58Decode,
  base58Encode,
  Base58Error,
  isValidPubkeyString,
} from "./base58.ts";

/** DER-обёртка SPKI для ed25519: 12 байт префикса + 32 байта ключа. */
const SPKI_ED25519_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

export type SiwsErrorCode =
  | "MalformedSignature"
  | "UnknownNonce"
  | "NonceExpired"
  | "NonceReused"
  | "InvalidSignature"
  | "NotModeratorAuthority";

export class SiwsError extends Error {
  // Явное поле: сокращённый модификатор в конструкторе несовместим со
  // strip-only режимом (см. README и test/no-parameter-properties).
  readonly code: SiwsErrorCode;

  constructor(code: SiwsErrorCode, message: string) {
    super(message);
    this.code = code;
  }
}

export interface SiwsParams {
  domain: string;
  uri: string;
  /** Сколько живёт nonce. Коротко: окно для перехвата должно быть узким. */
  nonceTtlMs: number;
  /** Сколько живёт сессия после успешной проверки. */
  sessionTtlMs: number;
}

export interface PendingNonce {
  nonce: string;
  /**
   * Адрес, заявленный при получении nonce.
   *
   * Привязка здесь, а не только в тексте сообщения: тогда сервер отдаёт клиенту
   * уже готовый текст, клиент не собирает его сам, и расхождение форматов
   * становится невозможным.
   */
  pubkey: string;
  issuedAt: string;
  expiresAt: number;
  used: boolean;
}

export interface Session {
  token: string;
  pubkey: string;
  expiresAt: number;
}

/**
 * Формирует текст, который подписывает кошелёк.
 *
 * Все поля, кроме адреса, берутся из записи, созданной сервером. Клиент не
 * может подменить ни nonce, ни время, ни домен: он возвращает только подпись.
 */
export function buildMessage(pubkey: string, pending: PendingNonce, params: SiwsParams): string {
  return [
    `${params.domain} запрашивает вход в ваш Solana-кошелёк:`,
    pubkey,
    "",
    "SixSec: подтверждение права модератора. Ничего не подписывается и не тратится.",
    "",
    `URI: ${params.uri}`,
    "Version: 1",
    `Nonce: ${pending.nonce}`,
    `Issued At: ${pending.issuedAt}`,
  ].join("\n");
}

export class SiwsVerifier {
  private readonly nonces = new Map<string, PendingNonce>();
  private readonly sessions = new Map<string, Session>();
  // Явное поле: ограничение `--experimental-strip-types` зафиксировано в
  // backend/README.md. Ошибку легко повторить — typecheck её не ловит,
  // поэтому есть отдельный тест no-parameter-properties.
  private readonly params: SiwsParams;

  constructor(params: SiwsParams) {
    this.params = params;
  }

  issueNonce(pubkey: string): PendingNonce {
    // Формат проверяется на входе: мусор не должен попадать в состояние сервера
    // и доживать до криптографии.
    if (!isValidPubkeyString(pubkey)) {
      throw new SiwsError("MalformedSignature", "адрес не является валидным Solana pubkey");
    }
    const now = Date.now();
    // Очистка при выдаче: без неё Map растёт неограниченно, и это готовый
    // отказ в обслуживании — nonce запрашивает кто угодно без авторизации.
    for (const [key, p] of this.nonces) {
      if (p.expiresAt <= now || p.used) this.nonces.delete(key);
    }
    const pending: PendingNonce = {
      nonce: randomUUID().replace(/-/g, ""),
      pubkey,
      issuedAt: new Date(now).toISOString(),
      expiresAt: now + this.params.nonceTtlMs,
      used: false,
    };
    this.nonces.set(pending.nonce, pending);
    return pending;
  }

  /**
   * Проверяет подпись и возвращает восстановленный адрес.
   *
   * Порядок намеренный: сначала криптография, потом роль. Иначе можно
   * перебором адресов выяснить, какой из них является модератором.
   */
  verify(pubkey: string, signatureB58: string, nonce: string): string {
    const pending = this.nonces.get(nonce);
    if (pending === undefined) throw new SiwsError("UnknownNonce", "неизвестный nonce");
    if (pending.used) throw new SiwsError("NonceReused", "nonce уже использован");
    if (pending.expiresAt <= Date.now()) {
      this.nonces.delete(nonce);
      throw new SiwsError("NonceExpired", "nonce истёк");
    }
    // Адрес обязан совпадать с заявленным при выдаче nonce. Без этой проверки
    // nonce, полученный для одного адреса, можно было бы предъявить от другого.
    if (pending.pubkey !== pubkey) {
      throw new SiwsError("InvalidSignature", "адрес не совпадает с заявленным при выдаче nonce");
    }

    let sig: Uint8Array;
    let rawKey: Uint8Array;
    try {
      sig = base58Decode(signatureB58);
      rawKey = base58Decode(pubkey);
    } catch (e) {
      throw new SiwsError(
        "MalformedSignature",
        e instanceof Base58Error ? e.message : "не удалось разобрать base58",
      );
    }
    if (rawKey.length !== 32) {
      throw new SiwsError("MalformedSignature", `публичный ключ: ${rawKey.length} байт, нужно 32`);
    }
    // ed25519-подпись всегда 64 байта. Проверка до verify — иначе ошибка
    // выглядела бы как «неверная подпись» и путала бы диагностику.
    if (sig.length !== 64) {
      throw new SiwsError("MalformedSignature", `подпись: ${sig.length} байт, нужно 64`);
    }

    const message = Buffer.from(buildMessage(pubkey, pending, this.params), "utf8");
    const keyObject = createPublicKey({
      key: Buffer.concat([SPKI_ED25519_PREFIX, Buffer.from(rawKey)]),
      format: "der",
      type: "spki",
    });

    let ok = false;
    try {
      ok = edVerify(null, message, keyObject, Buffer.from(sig));
    } catch {
      ok = false;
    }
    if (!ok) throw new SiwsError("InvalidSignature", "подпись не соответствует сообщению");

    // Одноразовость отмечается только после успешной проверки: иначе
    // злоумышленник сжигал бы nonce'ы, просто угадывая их.
    pending.used = true;
    return pubkey;
  }

  startSession(pubkey: string): Session {
    const session: Session = {
      // 32 байта энтропии; uuid для nonce здесь годится, для токена — нет.
      token: randomBytes(32).toString("base64url"),
      pubkey,
      expiresAt: Date.now() + this.params.sessionTtlMs,
    };
    this.sessions.set(session.token, session);
    return session;
  }

  /**
   * Проверка bearer-токена.
   *
   * `timingSafeEqual` — не паранойя: посимвольное сравнение строк позволяет
   * восстанавливать токен по времени ответа. Длины обязаны совпадать, иначе
   * функция бросает, поэтому сравниваем только при равной длине.
   */
  sessionOf(bearer: string | undefined): Session | null {
    if (bearer === undefined) return null;
    const token = bearer.replace(/^Bearer\s+/i, "").trim();
    if (token.length === 0) return null;
    const now = Date.now();
    for (const [key, s] of this.sessions) {
      if (s.expiresAt <= now) {
        this.sessions.delete(key);
        continue;
      }
      const a = Buffer.from(key);
      const b = Buffer.from(token);
      if (a.length === b.length && timingSafeEqual(a, b)) return s;
    }
    return null;
  }

  revoke(token: string): void {
    this.sessions.delete(token);
  }

  /** Размер состояний — для метрик и для проверки, что очистка работает. */
  stats(): { nonces: number; sessions: number } {
    return { nonces: this.nonces.size, sessions: this.sessions.size };
  }
}

/** Помощь для тестов и диагностики: base58-представление 32-байтного ключа. */
export function pubkeyToBase58(raw: Uint8Array): string {
  return base58Encode(raw);
}
