"use client";

/**
 * The field crew's offline half: priming their task list, and sending the
 * two writes they can make.
 *
 * Shares the storage, the identity, the reachability signal and the retry
 * cadence with estimator capture (`flush.ts`), and deliberately does NOT
 * share the flush itself. An estimate draft is one logical unit sent as a
 * three-call chain where step 2 needs step 1's id; these are independent
 * single-call writes with their own conflict rules. Forcing one function
 * over both would need a flag per difference.
 *
 * Design: `docs/superpowers/specs/2026-08-04-field-crew-offline-queue-design.md`.
 */

import type { OfflineIdentity } from "./identity";
import { identityKey } from "./identity";
import { primeDocuments } from "./service-worker";
import type { CachedTask, CrewReferenceData, QueueItem } from "./store";
import { deleteQueueItem, rememberIdentity, saveCrewReference, saveQueueItem } from "./store";

/**
 * `GET /my-tasks` is not cursor-paginated — it returns the caller's
 * assigned tasks in one response, capped at 200 by the route itself. So
 * there is no walk here, unlike `prime.ts`'s catalog.
 */
export async function fetchMyTasks(accessToken: string): Promise<CachedTask[]> {
  const response = await fetch("/api/my-tasks", {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail ?? "Failed to load your tasks");
  return data.items as CachedTask[];
}

/**
 * Store the crew member's tasks for offline use.
 *
 * The projects they may write a daily log against come from this same list
 * and need no request of their own: `MyTaskResponse` already carries
 * `project_id`/`project_name`, and the backend admits a field-crew caller
 * to exactly the projects they hold an assigned task on — so the set
 * derived from their tasks IS the set they are allowed to log against.
 */
export async function saveTasksForOffline(
  identity: OfflineIdentity,
  tasks: CachedTask[]
): Promise<CrewReferenceData> {
  const reference: CrewReferenceData = {
    identity: identityKey(identity),
    cached_at: new Date().toISOString(),
    tasks,
  };
  await saveCrewReference(reference);
  return reference;
}

/** Prime everything a cold start with no network needs. */
export async function primeCrewOffline(
  accessToken: string,
  identity: OfflineIdentity
): Promise<CrewReferenceData> {
  const tasks = await fetchMyTasks(accessToken);
  const reference = await saveTasksForOffline(identity, tasks);
  // Written here rather than at sign-in: this is the moment the device
  // starts holding this identity's data, and an offline cold start has no
  // token to re-derive it from.
  await rememberIdentity(identity);
  // Documents last, so a prime that fails halfway never leaves a device
  // that cold-starts into a screen with nothing behind it.
  await primeDocuments();
  return reference;
}

export type SendOutcome =
  | { kind: "sent" }
  | { kind: "deferred" }
  | { kind: "parked"; message: string; currentStatus?: string };

function messageFrom(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const structured = detail as { message?: string };
    if (structured.message) return structured.message;
  }
  return fallback;
}

/**
 * Send one queued write.
 *
 * The three outcomes are the same as the estimator flush's, and mean the
 * same things: **deferred** is "the request never arrived" and stays
 * queued; **parked** is "the server answered, and a person has to look";
 * **sent** removes it. Nothing here retries a parked item, ever.
 */
export async function sendQueueItem(item: QueueItem, accessToken: string): Promise<SendOutcome> {
  await saveQueueItem({ ...item, status: "sending", attention: null });

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${accessToken}`,
  };

  let response: Response;
  try {
    if (item.write.kind === "task_status") {
      response = await fetch(`/api/tasks/${item.write.task_id}`, {
        method: "PATCH",
        headers,
        body: JSON.stringify({
          status: item.write.status,
          // The value this crew member saw when they made the change. The
          // whole point of the guard: without it a change made hours ago
          // silently overwrites whatever happened since.
          expected_status: item.write.expected_status,
        }),
      });
    } else {
      response = await fetch(`/api/projects/${item.write.project_id}/daily-logs`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          log_date: item.write.log_date,
          weather: item.write.weather,
          notes: item.write.notes,
          // Repeated verbatim on every retry of this same log, which is
          // what makes sending it twice harmless — the route returns the
          // original row instead of writing a second one that could never
          // be deleted.
          client_reference: item.write.client_reference,
        }),
      });
    }
  } catch {
    // Never arrived. Back to `queued` so the next tick picks it up without
    // anyone pressing anything.
    await saveQueueItem({ ...item, status: "queued", attention: null });
    return { kind: "deferred" };
  }

  if (response.ok) {
    await deleteQueueItem(item.id);
    return { kind: "sent" };
  }

  const data = await response.json().catch(() => ({}));
  const detail = (data as { detail?: unknown }).detail;

  if (response.status === 409 && item.write.kind === "task_status") {
    const structured = (detail ?? {}) as { current_status?: string };
    const message = messageFrom(detail, "This task changed while you were offline.");
    await saveQueueItem({
      ...item,
      status: "needs_attention",
      attention: { message, current_status: structured.current_status },
    });
    return { kind: "parked", message, currentStatus: structured.current_status };
  }

  // 403 (the subscription lapsed), 404 (reassigned while they were out of
  // signal, or the project is gone), 422 — all of them park. None will
  // start succeeding on a retry, and a queue that kept trying would be the
  // silent failure this feature exists to remove, in a new place.
  const message = messageFrom(
    detail,
    response.status === 404
      ? "This is no longer yours to update — it may have been reassigned."
      : `The server refused this (${response.status}).`
  );
  await saveQueueItem({ ...item, status: "needs_attention", attention: { message } });
  return { kind: "parked", message };
}
