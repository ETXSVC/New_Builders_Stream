"use client";

/**
 * Who the cached data belongs to.
 *
 * A cache sits outside RLS entirely: whatever is in it is readable by
 * whoever holds the device, with no policy evaluated. Company scope
 * therefore has to be part of the KEY, and it cannot be the URL — one
 * person may hold memberships in several companies (migration 0031), so a
 * URL-keyed cache would serve company A's catalog while the user is acting
 * as company B. Nothing about `/api/catalog/items` distinguishes the two.
 *
 * The access token already names both halves, so there is no round trip
 * here: `sub` is the user and `default_company_id` is the company the
 * session is currently acting as — re-minted by `POST /auth/switch-company`,
 * which is why it is the ACTIVE company rather than a preference.
 *
 * The payload is read, never trusted. It decides which local bucket to
 * write to and read from; every authorization answer still comes from the
 * backend, which verifies the token's signature and the membership behind
 * it on every request.
 */

export interface OfflineIdentity {
  userId: string;
  companyId: string;
}

/**
 * The bucket key. Both halves, joined — a user id alone would let a company
 * switch read the previous company's catalog, which is the exact failure
 * the parent spec calls out as "most likely to be got wrong quietly."
 */
export function identityKey(identity: OfflineIdentity): string {
  return `${identity.userId}:${identity.companyId}`;
}

/**
 * Decode a JWT payload without verifying it.
 *
 * Signature verification is the backend's, and duplicating it here would
 * need the secret. This reads two claims to pick a local storage bucket;
 * forging them gains an attacker access to their own device's cache, which
 * they already have.
 */
export function readIdentity(accessToken: string | null): OfflineIdentity | null {
  if (!accessToken) return null;
  try {
    const payload = JSON.parse(atob(accessToken.split(".")[1]));
    const userId = payload.sub;
    const companyId = payload.default_company_id;
    if (typeof userId !== "string" || typeof companyId !== "string") return null;
    return { userId, companyId };
  } catch {
    return null;
  }
}

/**
 * The company id alone, for callers that only need the tenant — `AppShell`
 * passes it to `Nav`, which asks the API for the company's display name.
 *
 * Shared rather than re-derived: this app decoded the token in two places
 * and would now decode it in three, each free to disagree about which claim
 * names the active company.
 */
export function readCompanyId(accessToken: string | null): string {
  return readIdentity(accessToken)?.companyId ?? "";
}
