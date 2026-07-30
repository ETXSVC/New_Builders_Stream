"use client";

import * as React from "react";
import Link from "next/link";
import type { components } from "@/lib/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { platformFetch } from "@/lib/platform/client";

type TenantDetailData = components["schemas"]["TenantDetail"];

const TIERS = ["starter", "pro", "enterprise"] as const;

// Stripe's subscription statuses. `trialing` and `active` are the two the
// backend treats as write-enabled (`block_if_read_only`); the rest put the
// tenant into read-only, which is exactly why an operator would set one.
const STATUSES = [
  "trialing",
  "active",
  "past_due",
  "unpaid",
  "canceled",
  "incomplete",
  "incomplete_expired",
  "paused",
] as const;

const WRITE_ENABLED = new Set(["trialing", "active"]);

export function TenantDetailView({ companyId }: { companyId: string }) {
  const [tenant, setTenant] = React.useState<TenantDetailData | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState<string | null>(null);
  // Bumped on every successful write, and used as SubscriptionCard's `key` so
  // its inputs re-seed from the server's answer instead of keeping whatever
  // was typed.
  const [revision, setRevision] = React.useState(0);

  const load = React.useCallback(async () => {
    try {
      setTenant(await platformFetch<TenantDetailData>(`/api/platform/companies/${companyId}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load the tenant.");
    }
  }, [companyId]);

  React.useEffect(() => {
    // Deferred to a promise callback so no setState in load's path runs
    // synchronously inside the effect (react-hooks/set-state-in-effect) —
    // the same shape use-cursor-list.ts uses for the same reason.
    void Promise.resolve().then(() => load());
  }, [load]);

  /**
   * Applies a mutation and folds the response back in.
   *
   * The write routes return a TenantDetail with `child_company_ids` empty —
   * they do not re-query the tree — so replacing state wholesale would make
   * a tenant's branches vanish from the page until reload. Keep the list the
   * GET gave us.
   */
  const apply = React.useCallback(
    async (path: string, init: RequestInit, successMessage: string) => {
      if (busy) return;
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        const updated = await platformFetch<TenantDetailData>(path, init);
        setTenant((prev) => ({
          ...updated,
          child_company_ids: prev?.child_company_ids ?? updated.child_company_ids,
        }));
        setNotice(successMessage);
        setRevision((n) => n + 1);
      } catch (err) {
        setError(err instanceof Error ? err.message : "The change was not applied.");
      } finally {
        setBusy(false);
      }
    },
    [busy]
  );

  if (error && !tenant) {
    return (
      <p role="alert" className="text-sm text-red-600">
        {error}
      </p>
    );
  }
  if (!tenant) return <p className="text-sm text-slate-500">Loading…</p>;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <Link href="/platform" className="text-sm text-slate-500 hover:underline">
          ← All tenants
        </Link>
        <h1 className="text-2xl font-semibold">{tenant.name}</h1>
        <p className="font-mono text-xs text-slate-500">{tenant.company_id}</p>
      </div>

      {notice && (
        <p role="status" className="text-sm text-green-700">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {!tenant.is_root ? (
        <Card>
          <CardHeader>
            <CardTitle>This is a child branch</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 text-sm text-slate-600">
            <p>
              Entitlements are held by the root of this tenant&apos;s tree, and changing them there
              affects every branch in it — not just this one. The API refuses writes here rather
              than guessing which you meant.
            </p>
            {tenant.parent_id && (
              <Link
                href={`/platform/${tenant.parent_id}`}
                className="font-medium text-slate-900 hover:underline"
              >
                Go to the parent company →
              </Link>
            )}
          </CardContent>
        </Card>
      ) : (
        <>
          <SubscriptionCard key={revision} tenant={tenant} busy={busy} apply={apply} />
          <ModulesCard tenant={tenant} busy={busy} apply={apply} companyId={companyId} />
        </>
      )}

      {tenant.child_company_ids.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Child branches ({tenant.child_company_ids.length})</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-1 text-sm">
            {tenant.child_company_ids.map((id) => (
              <Link key={id} href={`/platform/${id}`} className="font-mono text-xs hover:underline">
                {id}
              </Link>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

type ApplyFn = (path: string, init: RequestInit, successMessage: string) => Promise<void>;

function SubscriptionCard({
  tenant,
  busy,
  apply,
}: {
  tenant: TenantDetailData;
  busy: boolean;
  apply: ApplyFn;
}) {
  const [tier, setTier] = React.useState(tenant.tier ?? "");
  const [status, setStatus] = React.useState(tenant.status ?? "");
  const [seats, setSeats] = React.useState(
    tenant.included_seats === null || tenant.included_seats === undefined
      ? ""
      : String(tenant.included_seats)
  );

  // No re-seeding effect here on purpose: the parent remounts this card via
  // a `key` after every successful write, which is React's own answer to
  // "reset state when the input changes" and avoids a setState-in-effect
  // that the lint rule (rightly) rejects.
  const dirty =
    tier !== (tenant.tier ?? "") ||
    status !== (tenant.status ?? "") ||
    seats !==
      (tenant.included_seats === null || tenant.included_seats === undefined
        ? ""
        : String(tenant.included_seats));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = {};
    if (tier && tier !== tenant.tier) body.tier = tier;
    if (status && status !== tenant.status) body.status = status;
    if (seats !== "" && Number(seats) !== tenant.included_seats) {
      body.included_seats = Number(seats);
    }
    if (Object.keys(body).length === 0) return;
    void apply(
      `/api/platform/companies/${tenant.company_id}/subscription`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      "Subscription updated."
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Subscription</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form onSubmit={submit} className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="tier">Tier</Label>
            <select
              id="tier"
              value={tier}
              onChange={(e) => setTier(e.target.value)}
              disabled={busy}
              className="h-10 rounded-md border border-slate-300 bg-white px-3 text-sm"
            >
              <option value="">(unchanged)</option>
              {TIERS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="status">Status</Label>
            <select
              id="status"
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              disabled={busy}
              className="h-10 rounded-md border border-slate-300 bg-white px-3 text-sm"
            >
              <option value="">(unchanged)</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                  {WRITE_ENABLED.has(s) ? "" : " — read-only"}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="seats">Included seats</Label>
            <Input
              id="seats"
              type="number"
              min={0}
              value={seats}
              onChange={(e) => setSeats(e.target.value)}
              disabled={busy}
              className="w-32"
            />
          </div>

          <Button type="submit" disabled={busy || !dirty}>
            Apply
          </Button>
        </form>

        <div className="rounded-md bg-slate-50 p-3 text-sm text-slate-600">
          <p>
            <span className="font-medium">Stripe control:</span>{" "}
            {tenant.manual_status_override ? (
              <>
                status is <span className="font-medium">operator-set</span>, and incoming Stripe
                events will not overwrite it.
              </>
            ) : (
              <>status follows Stripe.</>
            )}
          </p>
          <p className="mt-1 text-slate-500">
            Setting a status above takes control automatically — without that, the next routine
            <code className="mx-1 rounded bg-slate-200 px-1">customer.subscription.updated</code>
            would silently revert it.
          </p>
          {tenant.manual_status_override && (
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              disabled={busy}
              onClick={() =>
                void apply(
                  `/api/platform/companies/${tenant.company_id}/subscription`,
                  {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ clear_manual_status_override: true }),
                  },
                  "Status control handed back to Stripe."
                )
              }
            >
              Hand control back to Stripe
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function ModulesCard({
  tenant,
  busy,
  apply,
  companyId,
}: {
  tenant: TenantDetailData;
  busy: boolean;
  apply: ApplyFn;
  companyId: string;
}) {
  // One reason box for the next change rather than one per row: the note is
  // written to the target tenant's audit log, and an operator changing two
  // modules in one sitting is doing it for one reason.
  const [note, setNote] = React.useState("");

  function setOverride(module: string, enabled: boolean) {
    void apply(
      `/api/platform/companies/${companyId}/modules/${module}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled, note: note.trim() || null }),
      },
      `${module}: override set to ${enabled ? "granted" : "withheld"}.`
    );
  }

  function clearOverride(module: string) {
    void apply(
      `/api/platform/companies/${companyId}/modules/${module}`,
      { method: "DELETE" },
      `${module}: override cleared, now follows the tier.`
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Module entitlements</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-sm text-slate-600">
          An override is three-state. <span className="font-medium">Grant</span> opens a module the
          tier withholds, <span className="font-medium">Withhold</span> closes one the tier grants,
          and <span className="font-medium">Follow tier</span> removes the override entirely.
          &quot;Withhold&quot; and &quot;follow tier&quot; are not the same thing — collapsing them
          would make &quot;off&quot; unexpressible on a tier that includes the module.
        </p>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="note">Reason (optional, recorded in the audit log)</Label>
          <Input
            id="note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. paid add-on agreed with the customer"
            maxLength={500}
            disabled={busy}
          />
        </div>

        <div className="overflow-x-auto rounded-md border border-slate-200">
          <table className="w-full text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2 font-medium">Module</th>
                <th className="px-3 py-2 font-medium">By tier</th>
                <th className="px-3 py-2 font-medium">Override</th>
                <th className="px-3 py-2 font-medium">Effective</th>
                <th className="px-3 py-2 font-medium">Set</th>
              </tr>
            </thead>
            <tbody>
              {tenant.modules.map((entitlement) => (
                <tr
                  key={entitlement.module}
                  className="border-b border-slate-100 align-middle last:border-0"
                >
                  <td className="px-3 py-2">
                    <span className="font-medium">{entitlement.module}</span>
                    {entitlement.note && (
                      <span className="block text-xs text-slate-500">{entitlement.note}</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-slate-600">
                    {entitlement.allowed_by_tier ? "included" : "not included"}
                  </td>
                  <td className="px-3 py-2">
                    {entitlement.override === null || entitlement.override === undefined ? (
                      <span className="text-slate-400">none</span>
                    ) : entitlement.override ? (
                      <span className="font-medium text-green-700">granted</span>
                    ) : (
                      <span className="font-medium text-red-600">withheld</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {entitlement.effective ? (
                      <span className="font-medium text-green-700">on</span>
                    ) : (
                      <span className="font-medium text-slate-500">off</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex gap-1">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy || entitlement.override === true}
                        onClick={() => setOverride(entitlement.module, true)}
                      >
                        Grant
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy || entitlement.override === false}
                        onClick={() => setOverride(entitlement.module, false)}
                      >
                        Withhold
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={
                          busy || entitlement.override === null || entitlement.override === undefined
                        }
                        onClick={() => clearOverride(entitlement.module)}
                      >
                        Follow tier
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
