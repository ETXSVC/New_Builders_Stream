"use client";

/**
 * Sending a captured draft to the server, once there is a server to send it
 * to.
 *
 * Three calls with a dependency between them:
 *
 *   1. `POST /estimates`                  → returns the id steps 2 and 3 need
 *   2. `PUT /estimates/{id}/lines`        → carries `expected_unit_rate`
 *   3. `POST /estimates/{id}/calculate`   → totals
 *
 * Stored and replayed as ONE logical unit rather than three queued requests,
 * which is what removes the dependency graph: there is nothing to rewrite
 * with a predecessor's id, because the sequence runs here in order.
 *
 * ## Three outcomes, and the difference between them is the whole design
 *
 * - **flushed** — all three succeeded. The draft is deleted; the estimate on
 *   the server is now the record.
 * - **deferred** — the network failed. NOT a failure of the draft: it stays
 *   `captured` and flushes on the next attempt. This is the ordinary case
 *   for a device that is still out of signal.
 * - **parked** — the server refused, and a human has to settle it. The draft
 *   moves to `needs_attention` and **is never retried automatically**
 *   (design §8.3). At flush time there may be nobody looking at the screen,
 *   and adopting a rate requires a person; a queue that silently retried a
 *   409 forever would be the same silent failure this feature exists to
 *   remove, relocated.
 */

import { readSaveError } from "@/lib/estimates/rate-conflicts";
import type { CaptureDraft, DraftAttention } from "./store";
import { deleteDraft, saveDraft } from "./store";

export type FlushOutcome =
  | { kind: "flushed"; draftId: string; estimateId: string; total: string }
  | { kind: "deferred"; draft: CaptureDraft }
  | { kind: "parked"; draft: CaptureDraft };

function authHeaders(accessToken: string): Record<string, string> {
  return { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` };
}

async function park(draft: CaptureDraft, attention: DraftAttention): Promise<FlushOutcome> {
  const parked: CaptureDraft = { ...draft, status: "needs_attention", attention };
  await saveDraft(parked);
  return { kind: "parked", draft: parked };
}

async function defer(draft: CaptureDraft): Promise<FlushOutcome> {
  // Back to `captured`, not left `flushing`: a draft that stayed mid-flight
  // because the van drove out of signal must be picked up by the next
  // attempt without anyone pressing anything.
  const deferred: CaptureDraft = { ...draft, status: "captured", attention: null };
  await saveDraft(deferred);
  return { kind: "deferred", draft: deferred };
}

/**
 * A 403 means the tenant's tier or its per-module override moved while the
 * estimator was offline, and `require_module("estimation")` is on every
 * mutating estimate route. It will never succeed on retry, so it parks with
 * a message that says what to do about it rather than looking like a bug.
 */
function blockedMessage(status: number, detail: unknown): string {
  if (status === 403) {
    return typeof detail === "string" && detail
      ? `${detail} This draft is kept — it can be sent once the plan allows estimating again.`
      : "This company's plan no longer includes estimating. The draft is kept.";
  }
  return typeof detail === "string" && detail ? detail : `The server refused the draft (${status}).`;
}

export async function flushDraft(
  draft: CaptureDraft,
  accessToken: string
): Promise<FlushOutcome> {
  let working: CaptureDraft = { ...draft, status: "flushing", attention: null };
  await saveDraft(working);

  // --- Step 1: the estimate itself ---------------------------------------
  //
  // Skipped entirely when the draft already carries an id, which is how a
  // flush interrupted after step 1 resumes instead of creating a second
  // estimate against the same lead.
  if (!working.estimate_id) {
    let response: Response;
    try {
      response = await fetch("/api/estimates", {
        method: "POST",
        headers: authHeaders(accessToken),
        body: JSON.stringify({
          project_id: working.target.kind === "project" ? working.target.id : null,
          lead_id: working.target.kind === "lead" ? working.target.id : null,
          markup_profile_id: working.markup_profile_id,
        }),
      });
    } catch {
      return defer(working);
    }

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      return park(working, {
        kind: "blocked",
        message: blockedMessage(response.status, data.detail),
      });
    }

    // Persisted BEFORE step 2 is attempted. A tab closed between these two
    // lines would otherwise leave an empty estimate on the server that
    // nothing knows about — the "empty estimate lying around" the design
    // rules out as an acceptable outcome.
    working = { ...working, estimate_id: data.id as string };
    await saveDraft(working);
  }

  const estimateId = working.estimate_id as string;

  // --- Step 2: the lines, with the rates the estimator actually saw ------
  let linesResponse: Response;
  try {
    linesResponse = await fetch(`/api/estimates/${estimateId}/lines`, {
      method: "PUT",
      headers: authHeaders(accessToken),
      body: JSON.stringify({
        items: working.lines.map((line) => ({
          cost_catalog_item_id: line.cost_catalog_item_id,
          quantity: line.quantity,
          // The rate captured ON SITE, days ago. Without this the server
          // would copy whatever the catalog says now, and the estimate
          // would record a number nobody ever saw — the failure that made
          // offline capture unsafe to build before PR #128.
          expected_unit_rate: line.unit_rate,
        })),
      }),
    });
  } catch {
    return defer(working);
  }

  const linesData = await linesResponse.json().catch(() => ({}));
  if (!linesResponse.ok) {
    const { message, conflicts } = readSaveError(linesData.detail);
    if (linesResponse.status === 409 && conflicts.length > 0) {
      return park(working, { kind: "rate_conflict", message, conflicts });
    }
    return park(working, {
      kind: "blocked",
      message: blockedMessage(linesResponse.status, linesData.detail ?? message),
    });
  }

  // --- Step 3: totals ----------------------------------------------------
  let calcResponse: Response;
  try {
    calcResponse = await fetch(`/api/estimates/${estimateId}/calculate`, {
      method: "POST",
      headers: authHeaders(accessToken),
    });
  } catch {
    return defer(working);
  }

  const calcData = await calcResponse.json().catch(() => ({}));
  if (!calcResponse.ok) {
    return park(working, {
      kind: "blocked",
      message: blockedMessage(calcResponse.status, calcData.detail),
    });
  }

  // Deleted rather than kept as "flushed": the server now holds the record,
  // and a copy of it here would be one more device-resident set of the
  // company's rates with nothing left to do. The screen keeps the result in
  // memory for the session, with a link to the real estimate.
  await deleteDraft(working.id);
  return {
    kind: "flushed",
    draftId: working.id,
    estimateId,
    total: String(calcData.total ?? ""),
  };
}

/**
 * Discard a draft, taking any half-created estimate with it.
 *
 * The only path by which step 1 can leave an empty estimate on the server
 * permanently is a human deciding the draft is no longer wanted. So the
 * delete goes FIRST and a failure aborts the discard: a local record that
 * still knows the id is recoverable, and an orphaned empty estimate with
 * nothing pointing at it is not.
 */
export async function discardDraft(
  draft: CaptureDraft,
  accessToken: string | null
): Promise<{ ok: true } | { ok: false; error: string }> {
  if (draft.estimate_id) {
    if (!accessToken) {
      return {
        ok: false,
        error:
          "This draft already created an estimate on the server. Discarding it needs a connection, so it can be removed there too.",
      };
    }
    try {
      const response = await fetch(`/api/estimates/${draft.estimate_id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok && response.status !== 404) {
        return { ok: false, error: `Could not remove the estimate on the server (${response.status}).` };
      }
    } catch {
      return {
        ok: false,
        error: "Unable to reach the server, so the estimate it created cannot be removed yet.",
      };
    }
  }
  await deleteDraft(draft.id);
  return { ok: true };
}
