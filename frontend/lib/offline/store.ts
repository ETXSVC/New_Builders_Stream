"use client";

/**
 * The offline store: cached reference data, and captured drafts.
 *
 * IndexedDB rather than the service worker's HTTP cache, and that is the
 * central decision here rather than a storage preference. A `Cache` entry is
 * keyed by URL, and `/api/catalog/items` is the same URL for both companies
 * a dual-membership user belongs to — so an HTTP cache physically cannot
 * express "company A's catalog". Every record below carries an `identity`
 * (`lib/offline/identity.ts`), and every read is filtered by it.
 *
 * Two kinds of thing live here and they are cleared by different rules
 * (design §8.5):
 *
 * - **Reference data** — the company's catalog, its markup profiles, the
 *   leads and projects an estimate may be attached to. This is the
 *   commercially sensitive half: it is the company's pricing. Cleared on
 *   logout and on company switch, and removable on demand from the capture
 *   screen.
 * - **Drafts** — the estimator's own unsent work. NOT cleared on logout:
 *   deleting somebody's uncommitted work as a side effect of signing out
 *   loses it silently, at the moment they are least expecting anything
 *   destructive. They stay keyed by identity, so the next person to sign in
 *   on the device does not see them.
 *
 * No wrapper library: this is one database, three stores, and a handful of
 * gets and puts. `idb` is not in package.json and adding a dependency to
 * avoid forty lines of promise plumbing is a worse trade than writing them.
 */

import type { OfflineIdentity } from "./identity";
import { identityKey } from "./identity";
import type { RateConflict } from "@/lib/estimates/rate-conflicts";

const DB_NAME = "builders-stream-offline";
const DB_VERSION = 1;

const REFERENCE_STORE = "reference";
const DRAFT_STORE = "drafts";
const META_STORE = "meta";

/** The one meta row: who this device last held a session for. */
const IDENTITY_KEY = "identity";

export interface CachedCatalogItem {
  id: string;
  category: string;
  name: string;
  unit: string;
  unit_rate: string;
}

export interface CachedMarkupProfile {
  id: string;
  name: string;
}

/**
 * Something an estimate can be created against.
 *
 * `POST /estimates` takes exactly one of `project_id`/`lead_id` and verifies
 * the referenced row exists and is eligible, so neither can be conjured
 * client-side. Offline capture therefore works for "I am visiting a lead
 * that is already in the system" and not for "I met someone new on site" —
 * a real product limitation, stated on the screen rather than discovered at
 * flush.
 */
export interface CaptureTarget {
  kind: "project" | "lead";
  id: string;
  name: string;
}

export interface ReferenceData {
  /** `identityKey(...)` — the primary key, one record per identity. */
  identity: string;
  /** ISO 8601. Rendered on the screen so the estimator can see its age. */
  cached_at: string;
  catalog: CachedCatalogItem[];
  profiles: CachedMarkupProfile[];
  targets: CaptureTarget[];
}

export interface CapturedLine {
  cost_catalog_item_id: string;
  /** Denormalised so a draft reads correctly with the cache cleared. */
  name: string;
  unit: string;
  /**
   * The rate the estimator SAW when they captured this line. Sent as
   * `expected_unit_rate` at flush, which is what turns a catalog that moved
   * in the meantime into a visible 409 rather than silent re-pricing.
   */
  unit_rate: string;
  quantity: string;
}

/**
 * Why a draft stopped, when it stopped for a reason a human has to settle.
 *
 * `rate_conflict` is recoverable in place — the same old → new component the
 * online builder uses. `blocked` is everything the flush must not retry:
 * a 403 because the tenant's tier or module override moved while they were
 * offline, a lead that is no longer in an eligible state, a deleted project.
 * Retrying either forever would be the silent failure this feature exists to
 * remove, relocated into a queue.
 */
export type DraftAttention =
  | { kind: "rate_conflict"; message: string; conflicts: RateConflict[] }
  | { kind: "blocked"; message: string };

export type DraftStatus = "captured" | "flushing" | "needs_attention";

export interface CaptureDraft {
  id: string;
  identity: string;
  target: CaptureTarget;
  markup_profile_id: string;
  markup_profile_name: string;
  lines: CapturedLine[];
  status: DraftStatus;
  /**
   * Set the moment `POST /estimates` returns, and persisted BEFORE the lines
   * are attempted. That ordering is the whole answer to partial failure: a
   * flush interrupted between step 1 and step 2 resumes against the estimate
   * it already created instead of creating a second one (design §8.4).
   */
  estimate_id: string | null;
  captured_at: string;
  attention: DraftAttention | null;
}

/**
 * IndexedDB does not exist during server rendering, and these modules are
 * imported by client components that Next also renders on the server. Every
 * entry point below goes through this rather than assuming a browser.
 */
function unavailable(): boolean {
  return typeof indexedDB === "undefined";
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(REFERENCE_STORE)) {
        db.createObjectStore(REFERENCE_STORE, { keyPath: "identity" });
      }
      if (!db.objectStoreNames.contains(DRAFT_STORE)) {
        const drafts = db.createObjectStore(DRAFT_STORE, { keyPath: "id" });
        // Every draft read is "this identity's drafts" — never all of them.
        drafts.createIndex("by_identity", "identity", { unique: false });
      }
      if (!db.objectStoreNames.contains(META_STORE)) {
        db.createObjectStore(META_STORE, { keyPath: "key" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/**
 * One transaction, one operation, one promise.
 *
 * Resolves on the TRANSACTION's completion rather than the request's
 * success for writes — a request can succeed inside a transaction that then
 * aborts, and a caller told "saved" about a draft that was not saved is the
 * failure mode this whole feature exists to remove.
 */
async function withStore<T>(
  storeName: string,
  mode: IDBTransactionMode,
  operation: (store: IDBObjectStore) => IDBRequest
): Promise<T> {
  const db = await openDb();
  try {
    return await new Promise<T>((resolve, reject) => {
      const transaction = db.transaction(storeName, mode);
      const request = operation(transaction.objectStore(storeName));
      let result: T;
      request.onsuccess = () => {
        result = request.result as T;
      };
      transaction.oncomplete = () => resolve(result);
      transaction.onabort = () => reject(transaction.error);
      transaction.onerror = () => reject(transaction.error);
    });
  } finally {
    db.close();
  }
}

// --- Reference data -------------------------------------------------------

export async function saveReferenceData(data: ReferenceData): Promise<void> {
  if (unavailable()) return;
  await withStore<void>(REFERENCE_STORE, "readwrite", (store) => store.put(data));
}

export async function readReferenceData(identity: OfflineIdentity): Promise<ReferenceData | null> {
  if (unavailable()) return null;
  const record = await withStore<ReferenceData | undefined>(
    REFERENCE_STORE,
    "readonly",
    (store) => store.get(identityKey(identity))
  );
  return record ?? null;
}

/**
 * Drop every identity's reference data, not just the active one.
 *
 * Called on logout and on company switch. Scoping it to the identity being
 * left would leave the OTHER company's pricing on the device, which is the
 * half of the multi-membership problem that is easy to forget: switching
 * away from company A is exactly when A's catalog should stop being here.
 */
export async function clearReferenceData(): Promise<void> {
  if (unavailable()) return;
  await withStore<void>(REFERENCE_STORE, "readwrite", (store) => store.clear());
}

// --- Drafts ---------------------------------------------------------------

export async function saveDraft(draft: CaptureDraft): Promise<void> {
  if (unavailable()) return;
  await withStore<void>(DRAFT_STORE, "readwrite", (store) => store.put(draft));
}

export async function listDrafts(identity: OfflineIdentity): Promise<CaptureDraft[]> {
  if (unavailable()) return [];
  const drafts = await withStore<CaptureDraft[]>(DRAFT_STORE, "readonly", (store) =>
    store.index("by_identity").getAll(identityKey(identity))
  );
  // Newest first: a parked draft is the thing most likely to need attention,
  // and it is the most recent one that was being worked on.
  return drafts.sort((a, b) => b.captured_at.localeCompare(a.captured_at));
}

export async function deleteDraft(draftId: string): Promise<void> {
  if (unavailable()) return;
  await withStore<void>(DRAFT_STORE, "readwrite", (store) => store.delete(draftId));
}

// --- Remembered identity --------------------------------------------------

/**
 * Which identity this device last held a session for.
 *
 * An offline cold start has no access token — it cannot be refreshed without
 * the network — so nothing in memory says whose cache to read. This is the
 * only thing that does. Written whenever a token is present, so it tracks
 * company switches.
 */
export async function rememberIdentity(identity: OfflineIdentity): Promise<void> {
  if (unavailable()) return;
  await withStore<void>(META_STORE, "readwrite", (store) =>
    store.put({ key: IDENTITY_KEY, ...identity })
  );
}

export async function readRememberedIdentity(): Promise<OfflineIdentity | null> {
  if (unavailable()) return null;
  const record = await withStore<({ key: string } & OfflineIdentity) | undefined>(
    META_STORE,
    "readonly",
    (store) => store.get(IDENTITY_KEY)
  );
  if (!record) return null;
  return { userId: record.userId, companyId: record.companyId };
}

export async function forgetIdentity(): Promise<void> {
  if (unavailable()) return;
  await withStore<void>(META_STORE, "readwrite", (store) => store.delete(IDENTITY_KEY));
}
