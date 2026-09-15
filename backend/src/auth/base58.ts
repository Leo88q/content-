/**
 * Base58 (алфавит Bitcoin/Solana) для адресов и подписей.
 *
 * Своя реализация вместо пакета: needed всего две функции, а тянуть dependency
 * ради них в backend, где сейчас три зависимости, — неоправданно. Алгоритм
 * покрыт тестами, включая ведущие нули, которые чаще всего и ломают.
 */

const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const BASE = 58n;

const INDEX: Record<string, bigint> = {};
for (let i = 0; i < ALPHABET.length; i++) {
  INDEX[ALPHABET[i]!] = BigInt(i);
}

export function base58Encode(bytes: Uint8Array): string {
  let n = 0n;
  for (const b of bytes) n = n * 256n + BigInt(b);

  let out = "";
  while (n > 0n) {
    out = ALPHABET[Number(n % BASE)] + out;
    n /= BASE;
  }
  // Ведущие нулевые байты кодируются как '1'. Без этого адреса, начинающиеся
  // с нулевого байта, теряют длину при round-trip.
  for (const b of bytes) {
    if (b !== 0) break;
    out = "1" + out;
  }
  return out;
}

export class Base58Error extends Error {
  constructor(message: string) {
    super(message);
  }
}

export function base58Decode(text: string): Uint8Array {
  let n = 0n;
  for (const ch of text) {
    const v = INDEX[ch];
    if (v === undefined) {
      throw new Base58Error(`недопустимый символ base58: ${JSON.stringify(ch)}`);
    }
    n = n * BASE + v;
  }

  const bytes: number[] = [];
  while (n > 0n) {
    bytes.unshift(Number(n % 256n));
    n /= 256n;
  }
  let leadingZeros = 0;
  for (const ch of text) {
    if (ch !== "1") break;
    leadingZeros++;
  }
  return new Uint8Array([...new Array(leadingZeros).fill(0), ...bytes]);
}

/**
 * Проверка формата Solana-адреса.
 *
 * Только формат, не существование: 32 байта после декодирования и никаких
 * посторонних символов. Проверка нужна, чтобы мусор не доезжал до сравнения
 * с moderatorAuthority.
 */
export function isValidPubkeyString(text: string): boolean {
  if (text.length < 32 || text.length > 44) return false;
  try {
    return base58Decode(text).length === 32;
  } catch {
    return false;
  }
}
