import { createHmac } from "node:crypto";

/**
 * RFC 6238 TOTP, written out rather than pulled in as a dependency.
 *
 * The platform console refuses to sign anyone in without a second factor,
 * so an end-to-end test of its authenticated surface has to produce real
 * codes. That was the stated reason the surface went untested: "there is no
 * TOTP library in this project's Node dependencies."
 *
 * Adding one for test-only code buys a supply-chain dependency and a
 * lockfile entry; the algorithm is HMAC-SHA1 over a counter plus the
 * dynamic-truncation step below, and `node:crypto` already ships the only
 * hard part. Roughly thirty lines is the cheaper trade.
 *
 * This deliberately mirrors `backend/app/services/mfa.py`'s parameters —
 * SHA1, 6 digits, a 30-second step. If that file ever changes them, this
 * stops producing accepted codes and the console spec fails loudly, which
 * is the correct way for a duplicated constant to be discovered.
 */

/** Base32 (RFC 4648, no padding) — the encoding `pyotp` hands out. */
function base32Decode(input: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const cleaned = input.toUpperCase().replace(/=+$/, "").replace(/\s/g, "");
  let bits = 0;
  let value = 0;
  const out: number[] = [];

  for (const char of cleaned) {
    const index = alphabet.indexOf(char);
    if (index === -1) throw new Error(`Not base32: ${char}`);
    value = (value << 5) | index;
    bits += 5;
    // Emit a byte as soon as eight bits have accumulated, keeping the
    // remainder for the next round.
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

/**
 * A code for the current 30-second step, or a later one.
 *
 * `stepOffset` is not a convenience. `verify_totp_code`'s replay guard
 * refuses any timestep at or BEFORE the last one used successfully — which
 * is exactly the interception it exists to stop — so a test that activates
 * enrollment and then immediately signs in cannot reuse the same code.
 * Activation uses the current step and login uses the next, which the
 * backend still accepts (it allows one step of skew either side) while
 * satisfying the guard. `backend/tests/test_platform_admin.py` carries the
 * same helper for the same reason.
 */
export function totpCode(secret: string, stepOffset = 0): string {
  const counter = Math.floor(Date.now() / 1000 / 30) + stepOffset;

  const counterBytes = Buffer.alloc(8);
  // Node's writeBigUInt64BE avoids the 2^53 rounding a Number would hit —
  // irrelevant for current timestamps, correct regardless.
  counterBytes.writeBigUInt64BE(BigInt(counter));

  const digest = createHmac("sha1", base32Decode(secret)).update(counterBytes).digest();

  // Dynamic truncation (RFC 4226 §5.3): the low nibble of the last byte
  // selects a 4-byte window, and the top bit is masked off so the result is
  // read as a positive 31-bit integer on every platform.
  const offset = digest[digest.length - 1] & 0x0f;
  const binary =
    ((digest[offset] & 0x7f) << 24) |
    (digest[offset + 1] << 16) |
    (digest[offset + 2] << 8) |
    digest[offset + 3];

  return String(binary % 1_000_000).padStart(6, "0");
}

/**
 * Seconds left in the current step.
 *
 * Used to avoid starting a two-code exchange (activate, then log in with
 * the next step) right as the window rolls over — the kind of once-a-day
 * flake that is very hard to read from a CI log.
 */
export function secondsLeftInStep(): number {
  return 30 - (Math.floor(Date.now() / 1000) % 30);
}
