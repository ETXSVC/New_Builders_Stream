"use client";

/**
 * Filling the offline cache, once, because somebody asked.
 *
 * Four reads and a service-worker message. The reads are the ones the
 * capture screen cannot work without: the catalog (items are picked from
 * it), the markup profiles (one is selected per estimate), and the leads and
 * projects an estimate may be attached to — `POST /estimates` needs a real
 * id for one of them and neither can be invented client-side.
 */

import type { OfflineIdentity } from "./identity";
import { identityKey } from "./identity";
import type {
  CachedCatalogItem,
  CachedMarkupProfile,
  CaptureTarget,
  ReferenceData,
} from "./store";
import { rememberIdentity, saveReferenceData } from "./store";
import { primeDocuments } from "./service-worker";

/**
 * A lead can only carry an estimate once it has reached `estimating` on the
 * forward pipeline — `POST /estimates` returns 422 otherwise
 * (`_LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE` in `app/routers/estimates.py`).
 *
 * Filtered here, at priming time, rather than shown and rejected later:
 * offering an ineligible lead offline means the estimator captures a whole
 * estimate against it and finds out hours later, with no way to move the
 * work. Projects carry no equivalent rule — the route checks visibility
 * only — so they are all offered.
 */
const LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE = ["estimating", "qualified", "won"];

interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

/**
 * Walk a cursor-paginated endpoint to exhaustion.
 *
 * Deliberately NOT `useCursorAll`, and this is the one place in the app
 * where writing the loop again is right: that hook is a React hook with
 * component state and a stale-response guard, for a list being rendered.
 * This is a single awaited step inside one user-initiated action, with no
 * component to leave inconsistent and no second walk to be superseded by.
 * Calling a hook here is not possible; copying its machinery to imitate one
 * would add the state without the renderer.
 *
 * It inherits the same known limit the hook documents: the walk is
 * unbounded, so a ten-thousand-item catalog is four hundred requests to
 * prime. That is visible here — it happens under a button with a progress
 * message — rather than hidden behind a screen the user is waiting on.
 */
async function walkAll<T>(path: string, accessToken: string): Promise<T[]> {
  const all: T[] = [];
  let cursor: string | null = null;
  do {
    const search = new URLSearchParams();
    if (cursor) search.set("cursor", cursor);
    const response = await fetch(`${path}?${search}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail ?? `Failed to load ${path}`);
    }
    const page = data as CursorPage<T>;
    all.push(...page.items);
    cursor = page.next_cursor ?? null;
  } while (cursor);
  return all;
}

interface LeadRow {
  id: string;
  project_name: string;
  contact_name: string;
  status: string;
}

interface ProjectRow {
  id: string;
  name: string;
}

export interface PrimeProgress {
  (message: string): void;
}

/**
 * Prime everything, and report what it is doing while it does.
 *
 * Order matters at the end, not the start: the documents are cached LAST,
 * after the data is committed, so a prime that fails halfway never leaves a
 * device that cold-starts into a capture screen with nothing to capture
 * against.
 */
export async function primeOfflineData(
  accessToken: string,
  identity: OfflineIdentity,
  onProgress: PrimeProgress = () => {}
): Promise<ReferenceData> {
  onProgress("Loading the cost catalog…");
  const catalog = await walkAll<CachedCatalogItem>("/api/catalog/items", accessToken);

  onProgress("Loading markup profiles…");
  const profiles = await walkAll<CachedMarkupProfile>("/api/markup-profiles", accessToken);

  onProgress("Loading projects and leads…");
  const projects = await walkAll<ProjectRow>("/api/projects", accessToken);
  const leads = await walkAll<LeadRow>("/api/leads", accessToken);

  const targets: CaptureTarget[] = [
    ...projects.map((project): CaptureTarget => ({
      kind: "project",
      id: project.id,
      name: project.name,
    })),
    ...leads
      .filter((lead) => LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE.includes(lead.status))
      .map((lead): CaptureTarget => ({
        kind: "lead",
        id: lead.id,
        // Both, because a lead's own name IS the person: "Deck rebuild"
        // alone is not enough to pick the right one on site.
        name: `${lead.project_name} — ${lead.contact_name}`,
      })),
  ];

  const reference: ReferenceData = {
    identity: identityKey(identity),
    cached_at: new Date().toISOString(),
    catalog: catalog.map((item) => ({
      id: item.id,
      category: item.category,
      name: item.name,
      unit: item.unit,
      unit_rate: item.unit_rate,
    })),
    profiles: profiles.map((profile) => ({ id: profile.id, name: profile.name })),
    targets,
  };

  onProgress("Saving for offline use…");
  await saveReferenceData(reference);
  // Written here rather than on sign-in: this is the moment the device
  // starts holding this identity's data, and an offline cold start has no
  // token to re-derive it from.
  await rememberIdentity(identity);

  onProgress("Storing the capture screen…");
  await primeDocuments();

  return reference;
}
