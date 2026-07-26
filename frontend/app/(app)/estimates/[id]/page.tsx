"use client";

import * as React from "react";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { useLatestOnly } from "@/lib/use-latest-only";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { EstimateBuilder } from "@/components/estimates/EstimateBuilder";
import { PdfPanel } from "@/components/estimates/PdfPanel";
import { SigningPanel } from "@/components/esign/SigningPanel";
import { formatCurrency } from "@/lib/format";

interface LineItem {
  id: string;
  cost_catalog_item_id: string;
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
  const [profiles, setProfiles] = React.useState<MarkupProfileOption[]>([]);
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

  // A SECOND counter, deliberately not shared with `beginLoad` above: the two
  // loaders are independent, and one counter between them would mean starting
  // a profile load invalidates an in-flight estimate load (and vice versa),
  // silently dropping a result that was never superseded.
  const beginLoadProfiles = useLatestOnly();

  const loadProfiles = React.useCallback(async () => {
    if (!accessToken) return;
    const isCurrent = beginLoadProfiles();
    try {
      const all: MarkupProfileOption[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/markup-profiles?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (!response.ok) return;
        const data = await response.json();
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      if (!isCurrent()) return;
      setProfiles(all);
    } catch {
      // Non-blocking — the Select just stays empty if this fails.
    }
  }, [accessToken, beginLoadProfiles]);

  React.useEffect(() => {
    void Promise.resolve().then(() => {
      void load();
      void loadProfiles();
    });
  }, [load, loadProfiles]);

  async function handleMarkupChange(markupProfileId: string) {
    if (!accessToken || !estimate) return;
    const response = await fetch(`/api/estimates/${estimate.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({ markup_profile_id: markupProfileId }),
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
    if (!accessToken || !estimate || duplicating) return;
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
          items: estimate.line_items.map((li) => ({
            cost_catalog_item_id: li.cost_catalog_item_id,
            quantity: li.quantity,
          })),
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
                <span>Qty {li.quantity} @ {formatCurrency(li.unit_rate_snapshot)}</span>
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
