"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { formatCurrency } from "@/lib/format";
import { LineRows, type DraftLine } from "./LineRows";
import { CatalogItemPicker, type PickableCatalogItem } from "./CatalogItemPicker";
import { RateConflictNotice } from "./RateConflictNotice";
import { identityKey, readIdentity, type OfflineIdentity } from "@/lib/offline/identity";
import { useConnectionEvidence, useOfflineIdentity, useRetryTicker } from "@/lib/offline/hooks";
import { primeOfflineData } from "@/lib/offline/prime";
import { clearOfflineCaches } from "@/lib/offline/reset";
import { discardDraft, flushDraft } from "@/lib/offline/flush";
import {
  listDrafts,
  readReferenceData,
  saveDraft,
  type CaptureDraft,
  type CaptureTarget,
  type ReferenceData,
} from "@/lib/offline/store";

/**
 * Capturing an estimate on site, where there is no signal.
 *
 * Deliberately NOT the estimate builder. The builder calculates, exports a
 * PDF, sends for signature and takes an e-signature — none of which can work
 * offline, and none of which an estimator needs while standing in a
 * building. This screen does the five things that must happen there and
 * nothing else: pick what the estimate is against, pick a markup profile,
 * add catalog items with quantities, save it locally, and send it when there
 * is a network again.
 *
 * ## What it cannot do, and why that is stated on the screen
 *
 * It cannot create the lead or the project. `POST /estimates` requires one
 * of them and verifies the row exists and is eligible, so neither can be
 * conjured client-side. Offline capture therefore covers "I am visiting a
 * lead that is already in the system" and not "I met someone new on site" —
 * a real limitation, and one an estimator should read here rather than
 * discover at the far end of a day's work.
 *
 * ## Nothing is cached until somebody asks
 *
 * The catalog is the company's pricing, which is the one dataset a
 * competitor would want, and a cache sits outside RLS entirely. So there is
 * no background priming: "Make available offline" is a button, its result is
 * shown with the date it was taken, and "Remove offline data" takes it back
 * off the device (design §8.1).
 */

type TargetKey = string;

function targetKeyOf(target: CaptureTarget): TargetKey {
  return `${target.kind}:${target.id}`;
}

interface FlushedEstimate {
  draftId: string;
  estimateId: string;
  total: string;
}

export function CaptureScreen() {
  const { accessToken } = useAuth();
  // Identity, reachability and the retry cadence are shared with the field
  // crew's queue (`lib/offline/hooks.ts`) — two screens answering "are we
  // offline" separately is how they end up disagreeing.
  const { identity } = useOfflineIdentity();
  const { reachable, noteAttempt } = useConnectionEvidence();

  const [reference, setReference] = React.useState<ReferenceData | null>(null);
  const [drafts, setDrafts] = React.useState<CaptureDraft[]>([]);
  const [loaded, setLoaded] = React.useState(false);
  // Ticks only while something is still waiting to send, so a device with
  // no signal makes one attempt per interval and a screen with nothing
  // queued polls not at all.
  const waiting = loaded && drafts.some((draft) => draft.status !== "needs_attention");
  const { tick, retryNow } = useRetryTicker(waiting && !!accessToken);

  const [priming, setPriming] = React.useState(false);
  const [primeMessage, setPrimeMessage] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [flushed, setFlushed] = React.useState<FlushedEstimate[]>([]);

  // The capture form.
  const [targetKey, setTargetKey] = React.useState<TargetKey>("");
  const [markupProfileId, setMarkupProfileId] = React.useState("");
  const [lines, setLines] = React.useState<DraftLine[]>([]);
  const [search, setSearch] = React.useState("");

  // One flush at a time, whatever fires it. Two concurrent flushes of the
  // same draft would each run step 1 and create two estimates against the
  // same lead — the failure `estimate_id` persistence exists to prevent,
  // reintroduced from the other direction.
  const flushingRef = React.useRef(false);
  /**
   * Which drafts have been auto-attempted, and on which tick.
   *
   * Without it, a draft whose flush is `deferred` (the request never
   * arrived) returns to `captured`, the effect below re-runs on the changed
   * draft list, and it is attempted again immediately — a spin, not a
   * retry. Keyed by tick rather than cleared on a timer, so there is no
   * bookkeeping to reset; "Send now" clears its own draft and bypasses it.
   */
  const autoAttemptedRef = React.useRef<Map<string, number>>(new Map());

  const refreshDrafts = React.useCallback(async (who: OfflineIdentity) => {
    setDrafts(await listDrafts(who));
  }, []);

  React.useEffect(() => {
    if (!identity) return;
    void (async () => {
      setReference(await readReferenceData(identity));
      await refreshDrafts(identity);
      setLoaded(true);
    })();
  }, [identity, refreshDrafts]);

  // --- Flushing ----------------------------------------------------------

  const runFlush = React.useCallback(
    async (candidates: CaptureDraft[], who: OfflineIdentity, token: string, attemptTick: number) => {
      if (flushingRef.current || candidates.length === 0) return;
      flushingRef.current = true;
      try {
        for (const draft of candidates) {
          autoAttemptedRef.current.set(draft.id, attemptTick);
          const outcome = await flushDraft(draft, token);
          // The connection status, told by the only thing that knows: a
          // request that either arrived or did not.
          noteAttempt(outcome.kind !== "deferred");
          if (outcome.kind === "flushed") {
            setFlushed((prev) => [
              { draftId: outcome.draftId, estimateId: outcome.estimateId, total: outcome.total },
              ...prev,
            ]);
          }
        }
        await refreshDrafts(who);
      } finally {
        flushingRef.current = false;
      }
    },
    [noteAttempt, refreshDrafts]
  );

  React.useEffect(() => {
    if (!identity || !accessToken || !loaded) return;
    // Attempted whatever the connection is BELIEVED to be. Trying and
    // failing costs one rejected fetch and is the only reliable test; not
    // trying because a flag said offline is how work sits on a device next
    // to a working connection.
    //
    // `flushing` is included on purpose: a draft left in that state was
    // interrupted mid-send (the tab closed, the browser was killed), and it
    // is safe to resume because `estimate_id` was persisted before the
    // lines were attempted. Parked drafts are NOT here — those wait for a
    // human, which is the whole point of parking them.
    const pending = drafts.filter(
      (draft) =>
        (draft.status === "captured" || draft.status === "flushing") &&
        autoAttemptedRef.current.get(draft.id) !== tick
    );
    void runFlush(pending, identity, accessToken, tick);
  }, [accessToken, drafts, identity, loaded, tick, runFlush]);

  // --- Priming -----------------------------------------------------------

  async function handlePrime() {
    if (!accessToken || priming) return;
    const who = readIdentity(accessToken);
    if (!who) return;
    setPriming(true);
    setError(null);
    try {
      const primed = await primeOfflineData(accessToken, who, setPrimeMessage);
      setReference(primed);
      setPrimeMessage("Ready to work offline.");
    } catch (err) {
      setPrimeMessage(null);
      setError(err instanceof Error ? err.message : "Could not prepare offline data.");
    } finally {
      setPriming(false);
    }
  }

  async function handleRemoveOfflineData() {
    setPrimeMessage(null);
    setError(null);
    await clearOfflineCaches();
    setReference(null);
  }

  // --- The capture form --------------------------------------------------

  function handleAdd(item: PickableCatalogItem) {
    setLines((prev) => {
      if (prev.some((line) => line.cost_catalog_item_id === item.id)) return prev;
      return [
        ...prev,
        {
          key: item.id,
          cost_catalog_item_id: item.id,
          name: item.name,
          unit: item.unit,
          // The rate as of NOW, which is what makes the 409 at flush
          // meaningful: it is the number this estimator saw, and may have
          // said out loud to a customer.
          unit_rate: item.unit_rate,
          quantity: "1",
        },
      ];
    });
  }

  async function handleSaveDraft() {
    if (!identity || !reference) return;
    const target = reference.targets.find((candidate) => targetKeyOf(candidate) === targetKey);
    if (!target || !markupProfileId || lines.length === 0) {
      setError("Pick what this estimate is for, a markup profile, and at least one line.");
      return;
    }
    const profile = reference.profiles.find((candidate) => candidate.id === markupProfileId);
    setError(null);

    const draft: CaptureDraft = {
      id: crypto.randomUUID(),
      identity: identityKey(identity),
      target,
      markup_profile_id: markupProfileId,
      markup_profile_name: profile?.name ?? "",
      // Catalogued lines only, and deliberately so: a survey captured on
      // site records WHAT was measured, and the priced quote is produced by
      // the server once there is a connection. A free-form line
      // (migration 0035) carries a price the estimator typed, which is a
      // quoting decision rather than a survey one — `handleAdd` above is the
      // only way a line gets here, and it only ever adds catalog items, so
      // the non-null assertion below is the type system catching up with
      // that rather than a claim being made on faith.
      lines: lines.map((line) => ({
        cost_catalog_item_id: line.cost_catalog_item_id as string,
        name: line.name,
        unit: line.unit,
        unit_rate: line.unit_rate,
        quantity: line.quantity,
      })),
      status: "captured",
      estimate_id: null,
      captured_at: new Date().toISOString(),
      attention: null,
    };
    await saveDraft(draft);
    setLines([]);
    setTargetKey("");
    setMarkupProfileId("");
    await refreshDrafts(identity);
    // Try immediately rather than waiting out an interval — the estimator
    // may well be back in signal by the time they press save.
    retryNow();
  }

  // --- Acting on a saved draft -------------------------------------------

  async function handleSendNow(draft: CaptureDraft) {
    if (!identity || !accessToken) return;
    autoAttemptedRef.current.delete(draft.id);
    setError(null);
    await runFlush([draft], identity, accessToken, tick);
  }

  /**
   * Adopt the catalog's current rates into a parked draft — and stop there.
   *
   * Same policy as the online builder, for the same reason: the estimator
   * may have quoted the old number on site, so they see the new one, and the
   * send is theirs to press. The draft returns to `captured` rather than
   * flushing itself, which is what keeps "adopting a rate needs a human"
   * true when the alternative is a background queue doing it unwatched.
   */
  async function handleAdoptRates(draft: CaptureDraft) {
    if (!identity || draft.attention?.kind !== "rate_conflict") return;
    const current = new Map(
      draft.attention.conflicts.map((conflict) => [
        conflict.cost_catalog_item_id,
        conflict.current_unit_rate,
      ])
    );
    const updated: CaptureDraft = {
      ...draft,
      lines: draft.lines.map((line) =>
        current.has(line.cost_catalog_item_id)
          ? { ...line, unit_rate: current.get(line.cost_catalog_item_id) as string }
          : line
      ),
      status: "captured",
      attention: null,
    };
    // Marked as already attempted so the auto-flush does NOT pick it up on
    // the next tick. Adopting a rate is a decision; sending the estimate at
    // that rate is a second one, and this screen must not make it for them.
    // "Send now" clears this mark.
    autoAttemptedRef.current.set(updated.id, tick);
    await saveDraft(updated);
    await refreshDrafts(identity);
  }

  async function handleDiscard(draft: CaptureDraft) {
    if (!identity) return;
    setError(null);
    const result = await discardDraft(draft, accessToken);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    await refreshDrafts(identity);
  }

  // --- Rendering ---------------------------------------------------------

  const catalog = React.useMemo(() => {
    const items = reference?.catalog ?? [];
    const needle = search.trim().toLowerCase();
    if (!needle) return items;
    // Filtered here rather than by the API, which is unreachable — the
    // online panel searches server-side and this cannot.
    return items.filter(
      (item) =>
        item.name.toLowerCase().includes(needle) ||
        item.category.toLowerCase().includes(needle)
    );
  }, [reference, search]);

  const parked = drafts.filter((draft) => draft.status === "needs_attention");
  const pending = drafts.filter((draft) => draft.status !== "needs_attention");

  return (
    <main className="p-6 flex flex-col gap-6 max-w-4xl">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">On-site capture</h1>
        <span
          className={`text-xs rounded-full px-2 py-1 ${
            reachable ? "bg-slate-100 text-slate-600" : "bg-amber-100 text-amber-800"
          }`}
        >
          {reachable ? "Online" : "Offline — drafts are saved on this device"}
        </span>
      </div>

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      <section className="rounded-md border border-slate-200 p-4 flex flex-col gap-2">
        <h2 className="font-medium">Offline data</h2>
        <p className="text-sm text-slate-600">
          The cost catalog, markup profiles and the projects and leads you can attach an estimate
          to are stored on this device so this screen works with no signal. It holds your
          company&apos;s pricing — leave it off any device you would not want that on.
        </p>
        {reference ? (
          <p className="text-sm text-slate-600">
            Stored {new Date(reference.cached_at).toLocaleString()} · {reference.catalog.length}{" "}
            catalog items · {reference.targets.length} projects and leads
          </p>
        ) : (
          <p className="text-sm text-slate-600">Nothing is stored on this device yet.</p>
        )}
        {primeMessage && <p className="text-sm text-slate-600">{primeMessage}</p>}
        <div className="flex flex-wrap gap-2">
          <Button type="button" onClick={handlePrime} disabled={priming || !reachable || !accessToken}>
            {priming ? "Preparing…" : reference ? "Refresh offline data" : "Make available offline"}
          </Button>
          {reference && (
            <Button type="button" variant="outline" onClick={handleRemoveOfflineData}>
              Remove offline data
            </Button>
          )}
        </div>
        {!reachable && !reference && (
          <p className="text-sm text-amber-800">
            This device has no offline data, and preparing it needs a connection.
          </p>
        )}
      </section>

      {parked.length > 0 && (
        <section className="flex flex-col gap-3">
          <h2 className="font-medium">Needs your attention</h2>
          {parked.map((draft) => (
            <div
              key={draft.id}
              className="rounded-md border border-amber-300 bg-amber-50/40 p-4 flex flex-col gap-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium text-sm">{draft.target.name}</span>
                <span className="text-xs text-slate-600">
                  Captured {new Date(draft.captured_at).toLocaleString()}
                </span>
              </div>
              <p className="text-sm text-slate-700">{draft.attention?.message}</p>
              {draft.attention?.kind === "rate_conflict" && (
                <RateConflictNotice
                  conflicts={draft.attention.conflicts}
                  onAdopt={() => void handleAdoptRates(draft)}
                  note="Nothing has been sent. Using the new rates updates this draft so you can check the numbers, then send it yourself."
                />
              )}
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  size="sm"
                  onClick={() => void handleSendNow(draft)}
                  disabled={!reachable || !accessToken}
                >
                  Send now
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => void handleDiscard(draft)}
                >
                  Discard
                </Button>
              </div>
            </div>
          ))}
        </section>
      )}

      {reference && (
        <section className="flex flex-col gap-4">
          <h2 className="font-medium">Capture an estimate</h2>
          <p className="text-sm text-slate-600">
            An estimate is captured against a project or a lead that already exists. A new
            enquiry met on site has to be added as a lead once you are back in signal.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="capture-target">For</Label>
              <Select
                id="capture-target"
                value={targetKey}
                onChange={(e) => setTargetKey(e.target.value)}
              >
                <option value="">Select…</option>
                {reference.targets.map((target) => (
                  <option key={targetKeyOf(target)} value={targetKeyOf(target)}>
                    {target.kind === "lead" ? "Lead" : "Project"}: {target.name}
                  </option>
                ))}
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="capture-markup">Markup profile</Label>
              <Select
                id="capture-markup"
                value={markupProfileId}
                onChange={(e) => setMarkupProfileId(e.target.value)}
              >
                <option value="">Select…</option>
                {reference.profiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.name}
                  </option>
                ))}
              </Select>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <CatalogItemPicker
              items={catalog}
              search={search}
              onSearchChange={setSearch}
              onAdd={handleAdd}
              emptyMessage="No catalog items stored on this device."
            />
            <div className="flex flex-col gap-3">
              {/* No `onRateChange`: without it `LineRows` renders every rate
                  as read-only text, which is what this screen wants — it
                  captures a survey, and the rates it shows are the catalog's,
                  not the estimator's to set. */}
              <LineRows
                lines={lines}
                onQuantityChange={(key, quantity) =>
                  setLines((prev) =>
                    prev.map((line) => (line.key === key ? { ...line, quantity } : line))
                  )
                }
                onRemove={(key) => setLines((prev) => prev.filter((line) => line.key !== key))}
              />
              {/* No total beyond the subtotal above: overhead, profit and tax
                  are `POST /estimates/{id}/calculate`'s job, and a
                  client-side reimplementation that rounded at a different
                  step would disagree with the server by cents on the
                  document a customer signs. */}
              <p className="text-xs text-slate-500">
                Markup and the final total are calculated by the server when this is sent.
              </p>
              <Button type="button" onClick={() => void handleSaveDraft()}>
                Save draft
              </Button>
            </div>
          </div>
        </section>
      )}

      {pending.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="font-medium">Saved on this device</h2>
          {pending.map((draft) => (
            <div
              key={draft.id}
              className="rounded-md border border-slate-200 p-3 flex flex-wrap items-center justify-between gap-2 text-sm"
            >
              <span>
                {draft.target.name} · {draft.lines.length} line
                {draft.lines.length === 1 ? "" : "s"}
              </span>
              <span className="text-slate-500 text-xs">
                {draft.status === "flushing"
                  ? "Sending…"
                  : reachable
                    ? "Waiting to send"
                    : "Held until you are back in signal"}
              </span>
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  onClick={() => void handleSendNow(draft)}
                  disabled={!reachable || !accessToken}
                >
                  Send now
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => void handleDiscard(draft)}
                >
                  Discard
                </Button>
              </div>
            </div>
          ))}
        </section>
      )}

      {flushed.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="font-medium">Sent</h2>
          {flushed.map((result) => (
            <div key={result.draftId} className="text-sm flex items-center gap-2">
              {/* `precise`: this is the exact figure the server just
                  committed against work captured on site, and the estimator
                  may be reading it back to a customer. Whole-dollar
                  rounding is right for a list of many; it is wrong for the
                  one number that was just decided. */}
              <span>Estimate created — total {formatCurrency(result.total, { precise: true })}</span>
              <Link
                href={`/estimates/${result.estimateId}`}
                className="text-slate-600 underline hover:text-slate-900"
              >
                Open it
              </Link>
            </div>
          ))}
        </section>
      )}
    </main>
  );
}
