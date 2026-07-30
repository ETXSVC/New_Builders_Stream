"use client";

import * as React from "react";
import type { components } from "@/lib/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Modal } from "@/components/ui/modal";
import { platformFetch } from "@/lib/platform/client";

type TenantSummary = components["schemas"]["TenantSummary"];

const TIERS = ["starter", "pro", "enterprise"] as const;
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

/**
 * The common edits, without leaving the list.
 *
 * Name and subscription are two different backend routes with two different
 * privilege stories (one writes `companies`, the other `subscriptions`), so
 * this issues up to two requests and does the rename FIRST. If the second
 * fails the first still stands, which is why the error says which half
 * landed rather than a bare "failed" — an operator who retries blindly
 * would otherwise wonder why the name is already right.
 *
 * Deliberately does NOT carry module overrides or the delete. Overrides are
 * a five-row table that wants space, and taking a tenant out of service
 * should not be one careless click away from a seat-count change; both live
 * on the detail page.
 */
export function EditTenantModal({
  tenant,
  onClose,
  onSaved,
}: {
  tenant: TenantSummary | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  // Keyed by the caller (see TenantList) so these initialisers re-run for
  // each tenant rather than needing an effect to re-seed them.
  const [name, setName] = React.useState(tenant?.name ?? "");
  const [tier, setTier] = React.useState(tenant?.tier ?? "");
  const [status, setStatus] = React.useState(tenant?.status ?? "");
  const [seats, setSeats] = React.useState(
    tenant?.included_seats === null || tenant?.included_seats === undefined
      ? ""
      : String(tenant.included_seats)
  );
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  if (!tenant) return null;

  const seatsChanged = seats !== "" && Number(seats) !== tenant.included_seats;
  const subscriptionChanged =
    (tier !== "" && tier !== tenant.tier) ||
    (status !== "" && status !== tenant.status) ||
    seatsChanged;
  const nameChanged = name.trim() !== "" && name.trim() !== tenant.name;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !tenant) return;
    setBusy(true);
    setError(null);
    let renamed = false;
    try {
      if (nameChanged) {
        await platformFetch(`/api/platform/companies/${tenant.company_id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.trim() }),
        });
        renamed = true;
      }
      if (subscriptionChanged) {
        const body: Record<string, unknown> = {};
        if (tier !== "" && tier !== tenant.tier) body.tier = tier;
        if (status !== "" && status !== tenant.status) body.status = status;
        if (seatsChanged) body.included_seats = Number(seats);
        await platformFetch(`/api/platform/companies/${tenant.company_id}/subscription`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
      onSaved();
      onClose();
    } catch (err) {
      const detail = err instanceof Error ? err.message : "The change was not applied.";
      setError(
        renamed
          ? `The name was changed, but the subscription was not: ${detail}`
          : detail
      );
    } finally {
      setBusy(false);
    }
  }

  // Entitlements hang off the root, so the subscription half is meaningless
  // for a branch — the backend refuses it by name. Show why rather than
  // offering controls that cannot work.
  const isBranch = !tenant.is_root;

  return (
    <Modal
      open
      onClose={onClose}
      title={`Edit ${tenant.name}`}
      description={
        isBranch
          ? "A child branch. Its entitlements are held by the root of its tree."
          : undefined
      }
      footer={
        <>
          <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            type="submit"
            form="edit-tenant-form"
            disabled={busy || (!nameChanged && !subscriptionChanged)}
          >
            {busy ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <form id="edit-tenant-form" onSubmit={submit} className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="edit-name">Company name</Label>
          <Input
            id="edit-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={255}
            disabled={busy}
          />
        </div>

        {!isBranch && (
          <>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="edit-tier">Tier</Label>
              <select
                id="edit-tier"
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
              <Label htmlFor="edit-status">Status</Label>
              <select
                id="edit-status"
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
              <p className="text-xs text-slate-500">
                Setting a status takes it out of Stripe&apos;s hands until you hand it back on
                the tenant&apos;s own page.
              </p>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="edit-seats">Included seats</Label>
              <Input
                id="edit-seats"
                type="number"
                min={0}
                value={seats}
                onChange={(e) => setSeats(e.target.value)}
                disabled={busy}
                className="w-32"
              />
            </div>
          </>
        )}

        {error && (
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        )}
      </form>
    </Modal>
  );
}
