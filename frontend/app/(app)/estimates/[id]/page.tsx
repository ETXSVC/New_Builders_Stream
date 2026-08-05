"use client";

import * as React from "react";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { useLatestOnly } from "@/lib/use-latest-only";
import { useCursorAll } from "@/lib/use-cursor-list";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { EstimateBuilder } from "@/components/estimates/EstimateBuilder";
import { PdfPanel } from "@/components/estimates/PdfPanel";
import { SigningPanel } from "@/components/esign/SigningPanel";
import { formatCurrency } from "@/lib/format";

interface LineItem {
  id: string;
  // null on a free-form line (migration 0035) — the estimator's own
  // description and unit stand in for the catalog item's.
  cost_catalog_item_id: string | null;
  description: string | null;
  unit: string | null;
  quantity: string;
  unit_rate_snapshot: string;
  line_total: string;
}

interface CategorySubtotal {
  category: string;
  subtotal: string;
}

interface Estimate {
  id: string;
  status: string;
  pdf_status: string;
  total: string | null;
  markup_profile_id: string;
  esignature_id: string | null;
  project_id: string | null;
  lead_id: string | null;
  line_items: LineItem[];
  // Optimistic-concurrency token (backend: app/services/concurrency.py).
  // Held as the raw string from the API and passed back VERBATIM — parsing it
  // into a Date would truncate Postgres's microseconds to milliseconds and the
  // comparison would never match again.
  updated_at: string;
}

interface MarkupProfileOption {
  id: string;
  name: string;
}

interface Esignature {
  signer_name: string;
  signer_email: string;
  signed_at: string;
  ip_address: string;
}

export default function EstimateDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { accessToken, role } = useAuth();
  const [estimate, setEstimate] = React.useState<Estimate | null>(null);
  const [breakdown, setBreakdown] = React.useState<CategorySubtotal[]>([]);
  const [esignature, setEsignature] = React.useState<Esignature | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [duplicating, setDuplicating] = React.useState(false);

  const canEdit = role === "admin" || role === "project_manager";

  const beginLoad = useLatestOnly();

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    const isCurrent = beginLoad();
    try {
      const response = await fetch(`/api/estimates/${id}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      // Every setState below is gated on this load still being the newest.
      // `id` changes under a mounted component — "Duplicate as new draft"
      // routes from the approved estimate straight to the new one — so the
      // outgoing estimate's response can land after the incoming one's and
      // put the OLD estimate back on screen under the NEW url. The visible
      // symptom is a fresh draft rendering as read-only, because the stale
      // approved record it reverted to has no editable line inputs.
      if (!isCurrent()) return;
      if (!response.ok) {
        setError(data.detail ?? "Failed to load estimate");
        return;
      }
      setEstimate(data);
      if (data.esignature_id) {
        const esigResponse = await fetch(`/api/esignatures/${data.esignature_id}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (esigResponse.ok) {
          const esig = await esigResponse.json();
          if (!isCurrent()) return;
          setEsignature(esig);
        }
      }
    } catch {
      if (!isCurrent()) return;
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, beginLoad, id]);

  // The hook keeps its own generation counter, independent of `beginLoad`
  // above — which is what the hand-written loader used a SECOND
  // `useLatestOnly` to achieve. Sharing one counter between the two would
  // mean starting a profile load invalidates an in-flight estimate load
  // (and vice versa), silently dropping a result that was never superseded.
  //
  // `error` is deliberately not destructured: loading the profiles is
  // non-blocking, and the Select simply stays empty if it fails.
  const { items: profiles } = useCursorAll<MarkupProfileOption>({
    path: "/api/markup-profiles",
    label: "markup profiles",
  });

  React.useEffect(() => {
    void Promise.resolve().then(() => {
      void load();
    });
  }, [load]);

  async function handleMarkupChange(markupProfileId: string) {
    if (!accessToken || !estimate) return;
    const response = await fetch(`/api/estimates/${estimate.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({
        markup_profile_id: markupProfileId,
        expected_updated_at: estimate.updated_at,
      }),
    });
    if (response.ok) void load();
  }

  async function handleDelete() {
    if (!accessToken || !estimate) return;
    const response = await fetch(`/api/estimates/${estimate.id}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (response.status === 204) router.push("/estimates");
  }

  async function handleSendForSignature() {
    if (!accessToken || !estimate) return;
    const response = await fetch(`/api/estimates/${estimate.id}/send-for-signature`, {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    const data = await response.json();
    if (!response.ok) {
      setError(data.detail ?? "Failed to send for signature");
      return;
    }
    void load();
  }

  async function handleDuplicate() {
    if (duplicating) return;
    if (!accessToken || !estimate) {
      // Never a silent return. The guard is real — a click can land while the
      // session is being refreshed, and firing the two writes below without a
      // token would 401 halfway through and leave an empty estimate behind —
      // but "nothing happened, and nothing said so" is the one outcome a
      // person cannot act on. They press the button again, it does nothing
      // again, and there is no evidence anywhere that a click occurred.
      setError("Still getting ready — try that again in a moment.");
      return;
    }
    setDuplicating(true);
    setError(null);
    try {
      const createResponse = await fetch("/api/estimates", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          markup_profile_id: estimate.markup_profile_id,
          project_id: estimate.project_id,
          lead_id: estimate.lead_id,
        }),
      });
      const created = await createResponse.json();
      if (!createResponse.ok) {
        setError(created.detail ?? "Failed to duplicate estimate");
        return;
      }
      const linesResponse = await fetch(`/api/estimates/${created.id}/lines`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          // Two shapes, because migration 0035 made `cost_catalog_item_id`
          // nullable and the API's `_exactly_one_shape` validator rejects a
          // line that is neither. Sending `{cost_catalog_item_id: null,
          // quantity}` for a free-form line 422s the whole BATCH, so one
          // Miscellaneous line made "Duplicate as new draft" fail outright on
          // an estimate that was otherwise fine.
          //
          // A catalogued line deliberately does NOT carry its rate: it is
          // re-priced from the catalog at write time, which is correct for a
          // new draft — the copy should quote today's prices, not the ones
          // the original was signed at. A free-form line has no catalog item
          // to re-read, so its rate travels with it, and that is the only way
          // it can survive the copy at all.
          items: estimate.line_items.map((li) =>
            li.cost_catalog_item_id !== null
              ? { cost_catalog_item_id: li.cost_catalog_item_id, quantity: li.quantity }
              : {
                  description: li.description,
                  unit: li.unit,
                  unit_rate: li.unit_rate_snapshot,
                  quantity: li.quantity,
                }
          ),
        }),
      });
      const linesData = await linesResponse.json();
      if (!linesResponse.ok) {
        setError(linesData.detail ?? "Failed to copy line items to the new estimate");
        return;
      }
      router.push(`/estimates/${created.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setDuplicating(false);
    }
  }

  if (!estimate) {
    return (
      <main className="p-6">
        {error ? <p role="alert" className="text-sm text-red-600">{error}</p> : <p className="text-sm text-slate-500">Loading…</p>}
      </main>
    );
  }

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Estimate</h1>
        <StatusBadge status={estimate.status} />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {estimate.status === "draft" && (
        <>
          {canEdit && (
            <div className="flex items-center gap-2">
              <Select
                aria-label="Markup profile"
                className="w-56"
                value={estimate.markup_profile_id}
                onChange={(e) => handleMarkupChange(e.target.value)}
              >
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </Select>
              <Button type="button" variant="outline" onClick={handleDelete}>
                Delete
              </Button>
              <Button
                type="button"
                onClick={handleSendForSignature}
                disabled={estimate.total === null}
                title={estimate.total === null ? "Save & calculate before sending" : undefined}
              >
                Send for signature
              </Button>
            </div>
          )}
          <EstimateBuilder
            // `key` forces a fresh builder whenever the estimate identity
            // changes. Without it, navigating between estimates without
            // unmounting the page — which is exactly what "Duplicate as new
            // draft" does, `router.push`ing to a new id on the same route —
            // keeps the previous builder instance alive, and its `lines` come
            // from a `useState` INITIALIZER that only ever runs on mount. A
            // changed `initialLines` prop is therefore ignored, so whatever
            // the builder was first seeded with is what it shows forever.
            //
            // That turns any transient bad read into a permanent one: mount
            // once with an estimate whose `line_items` had not landed yet and
            // the builder stays empty for good, with no amount of waiting or
            // re-fetching recovering it.
            //
            // `key` rather than an effect that re-seeds on `initialLines`:
            // re-seeding on the prop would also fire while someone is midway
            // through editing quantities and would discard their input. The
            // identity of the estimate is the only thing that should reset
            // this state, and that is precisely what `key` expresses.
            key={estimate.id}
            estimateId={estimate.id}
            initialLines={estimate.line_items}
            onSaved={(total, categoryBreakdown) => {
              setEstimate((prev) => (prev ? { ...prev, total } : prev));
              setBreakdown(categoryBreakdown);
            }}
          />
          {breakdown.length > 0 && (
            <div className="text-sm text-slate-600">
              {breakdown.map((b) => (
                <div key={b.category} className="flex justify-between">
                  <span>{b.category}</span>
                  <span>{formatCurrency(b.subtotal)}</span>
                </div>
              ))}
            </div>
          )}
          {/* PDF export (Decision 5) is a draft-state header action per
              Decision 3 — available as soon as the estimate has a
              calculated total, same "Save & calculate before sending" gate
              already used for the Send for signature button above. */}
          {estimate.total !== null && (
            <PdfPanel estimateId={estimate.id} pdfStatus={estimate.pdf_status} canExport={canEdit} />
          )}
        </>
      )}

      {estimate.status !== "draft" && (
        <div className="flex flex-col gap-4">
          <p className="text-lg font-semibold">{formatCurrency(estimate.total)}</p>
          <ul className="flex flex-col gap-1 text-sm">
            {estimate.line_items.map((li) => (
              <li key={li.id} className="flex justify-between">
                <span>
                  {/* A catalogued line has never shown a name here — this
                      view has no catalog lookup — but a free-form line
                      carries its own description, so show it rather than
                      leave the row as a bare quantity. */}
                  {li.description ? `${li.description} — ` : ""}
                  Qty {li.quantity} @ {formatCurrency(li.unit_rate_snapshot)}
                </span>
                <span>{formatCurrency(li.line_total)}</span>
              </li>
            ))}
          </ul>
          {breakdown.length > 0 && (
            <div className="text-sm text-slate-600">
              {breakdown.map((b) => (
                <div key={b.category} className="flex justify-between">
                  <span>{b.category}</span>
                  <span>{formatCurrency(b.subtotal)}</span>
                </div>
              ))}
            </div>
          )}

          <PdfPanel estimateId={estimate.id} pdfStatus={estimate.pdf_status} canExport={canEdit} />

          {estimate.status === "sent" && role === "client" && accessToken && (
            <SigningPanel
              approveUrl={`/api/estimates/${estimate.id}/approve`}
              rejectUrl={`/api/estimates/${estimate.id}/reject`}
              accessToken={accessToken}
              onDone={load}
            />
          )}
          {estimate.status === "sent" && role !== "client" && (
            <p className="text-sm text-slate-500">Waiting for the client&apos;s signature.</p>
          )}

          {estimate.status === "approved" && esignature && (
            <div className="text-sm border border-slate-200 rounded-md p-3">
              <p className="font-medium">Signed</p>
              <p>{esignature.signer_name} ({esignature.signer_email})</p>
              <p className="text-slate-500">
                {new Date(esignature.signed_at).toLocaleString()} · {esignature.ip_address}
              </p>
            </div>
          )}

          {estimate.status === "rejected" && (
            <p className="text-sm text-red-600">This estimate was rejected by the client.</p>
          )}

          {canEdit && (estimate.status === "approved" || estimate.status === "rejected") && (
            <Button type="button" variant="outline" onClick={handleDuplicate} disabled={duplicating}>
              {duplicating ? "Duplicating…" : "Duplicate as new draft"}
            </Button>
          )}
        </div>
      )}
    </main>
  );
}
