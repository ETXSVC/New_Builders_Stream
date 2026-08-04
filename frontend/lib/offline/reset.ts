"use client";

/**
 * Taking the company's data back off the device.
 *
 * Called from three places, and the third is the one that is easy to miss:
 * signing out, switching company, and the capture screen's own "Remove
 * offline data" button.
 *
 * The company switch matters because RLS stops at the network boundary. One
 * person may hold memberships in several companies (migration 0031), and a
 * cache that survived a switch would be company A's pricing sitting on a
 * device now acting as company B — visible with no policy evaluated,
 * because a cache has already left the boundary where policies are.
 *
 * **Drafts are deliberately not cleared** (design §8.5). They are the
 * estimator's own unsent work, they are keyed by identity so the next person
 * to sign in cannot see them, and deleting somebody's uncommitted work as a
 * side effect of pressing "Log out" loses it silently.
 */

import { clearServiceWorkerCaches } from "./service-worker";
import { clearReferenceData, forgetIdentity } from "./store";

export async function clearOfflineCaches(): Promise<void> {
  // Sequential rather than Promise.all: the worker's caches hold the
  // document, and a failure clearing IndexedDB should not leave the
  // document cached with nothing behind it. Neither call throws in
  // practice, but the ordering makes the intent readable.
  await clearReferenceData();
  await forgetIdentity();
  await clearServiceWorkerCaches();
}
