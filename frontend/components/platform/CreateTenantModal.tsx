"use client";

import * as React from "react";
import type { components } from "@/lib/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Modal } from "@/components/ui/modal";
import { platformFetch } from "@/lib/platform/client";

type CreateResponse = components["schemas"]["TenantCreateResponse"];

const TIERS = ["starter", "pro", "enterprise"] as const;

/**
 * Onboard a customer: company, owner, 14-day trial, in one transaction.
 *
 * Two screens rather than one, and the second is the reason this is a
 * modal at all. The backend returns the owner's password EXACTLY ONCE — it
 * is generated there, stored only as an Argon2id hash, and is not in the
 * audit log or the tenant detail. So the success state cannot be a toast
 * that disappears: it has to be something the operator dismisses
 * deliberately, having copied the credential first.
 */
export function CreateTenantModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [companyName, setCompanyName] = React.useState("");
  const [ownerEmail, setOwnerEmail] = React.useState("");
  const [ownerName, setOwnerName] = React.useState("");
  const [tier, setTier] = React.useState<string>("pro");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [created, setCreated] = React.useState<CreateResponse | null>(null);
  const [copied, setCopied] = React.useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setCreated(
        await platformFetch<CreateResponse>("/api/platform/companies", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            company_name: companyName.trim(),
            owner_email: ownerEmail.trim(),
            owner_full_name: ownerName.trim(),
            tier,
          }),
        })
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "The tenant was not created.");
    } finally {
      setBusy(false);
    }
  }

  /**
   * Closing after a successful create refreshes the list AND resets this
   * form, so reopening it does not show the previous customer's details —
   * or, worse, their password.
   */
  function finish() {
    const didCreate = created !== null;
    setCompanyName("");
    setOwnerEmail("");
    setOwnerName("");
    setTier("pro");
    setCreated(null);
    setCopied(false);
    setError(null);
    onClose();
    if (didCreate) onCreated();
  }

  if (created) {
    return (
      <Modal
        open={open}
        onClose={finish}
        title={`${created.tenant.name} is live`}
        description="Give the owner these details. The password is not stored anywhere you can read it again."
        footer={<Button onClick={finish}>Done</Button>}
      >
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Owner
            </span>
            <span className="text-sm">{created.owner_email}</span>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
              One-time password
            </span>
            <div className="flex items-center gap-2">
              <code className="flex-1 break-all rounded bg-slate-100 px-2 py-1.5 font-mono text-sm">
                {created.temporary_password}
              </code>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  void navigator.clipboard
                    .writeText(created.temporary_password)
                    .then(() => setCopied(true))
                    // Clipboard access can be refused (permissions, or an
                    // insecure origin). The code is on screen either way, so
                    // say copying failed rather than claiming it worked.
                    .catch(() => setError("Could not copy — select the password above instead."));
                }}
              >
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
          </div>
          <p className="rounded-md bg-amber-50 p-3 text-sm text-amber-900">
            This is shown once. If it is lost, the owner has to use the ordinary
            password-reset flow — there is no way to retrieve it from here.
          </p>
          {error && (
            <p role="alert" className="text-sm text-red-600">
              {error}
            </p>
          )}
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      open={open}
      onClose={finish}
      title="New tenant"
      description="Creates a root company, its owner, and a 14-day trial."
      footer={
        <>
          <Button type="button" variant="ghost" onClick={finish} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="create-tenant-form" disabled={busy}>
            {busy ? "Creating…" : "Create tenant"}
          </Button>
        </>
      }
    >
      <form id="create-tenant-form" onSubmit={submit} className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="new-company-name">Company name</Label>
          <Input
            id="new-company-name"
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            maxLength={255}
            disabled={busy}
            required
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="new-owner-name">Owner name</Label>
          <Input
            id="new-owner-name"
            value={ownerName}
            onChange={(e) => setOwnerName(e.target.value)}
            maxLength={255}
            disabled={busy}
            required
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="new-owner-email">Owner email</Label>
          <Input
            id="new-owner-email"
            type="email"
            value={ownerEmail}
            onChange={(e) => setOwnerEmail(e.target.value)}
            disabled={busy}
            required
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="new-tier">Tier</Label>
          <select
            id="new-tier"
            value={tier}
            onChange={(e) => setTier(e.target.value)}
            disabled={busy}
            className="h-10 rounded-md border border-slate-300 bg-white px-3 text-sm"
          >
            {TIERS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <p className="text-xs text-slate-500">
            The trial starts here; modules follow the tier until an override says otherwise.
          </p>
        </div>
        {error && (
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        )}
      </form>
    </Modal>
  );
}
