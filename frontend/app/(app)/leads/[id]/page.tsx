"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { LeadStatusPipeline } from "@/components/leads/LeadStatusPipeline";
import { CommunicationLog } from "@/components/leads/CommunicationLog";
import { LeadForm, leadPayload, LeadFormValues } from "@/components/leads/LeadForm";
import { LEAD_TRANSITIONS, labelFor } from "@/lib/state-machines";
import { formatCurrency } from "@/lib/format";

interface Lead {
  id: string;
  contact_name: string;
  project_name: string;
  email: string;
  phone: string | null;
  status: string;
  estimated_value: string | null;
  project_type: string;
  notes: string | null;
}

export default function LeadDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { accessToken } = useAuth();
  const [lead, setLead] = React.useState<Lead | null>(null);
  const [editing, setEditing] = React.useState(false);
  const [wonBanner, setWonBanner] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/leads/${id}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load lead");
        return;
      }
      setLead(data);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, id]);

  React.useEffect(() => {
    load();
  }, [load]);

  async function patchLead(body: unknown, onSuccess?: (updated: Lead) => void) {
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/leads/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to update lead");
        return;
      }
      setLead(data);
      onSuccess?.(data);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (!lead) {
    return (
      <main className="p-6">
        {error ? (
          <p role="alert" className="text-sm text-red-600">{error}</p>
        ) : (
          <p className="text-sm text-slate-500">Loading…</p>
        )}
      </main>
    );
  }

  const nextStatuses = LEAD_TRANSITIONS[lead.status] ?? [];

  return (
    <main className="p-6 flex flex-col gap-6 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{lead.contact_name}</h1>
        <StatusBadge status={lead.status} />
      </div>
      <p className="text-sm text-slate-600 -mt-4">
        {lead.project_name} · {lead.project_type} · {formatCurrency(lead.estimated_value)} · {lead.email}
        {lead.phone ? ` · ${lead.phone}` : ""}
      </p>

      <LeadStatusPipeline status={lead.status} />

      {wonBanner && (
        <p className="text-sm text-green-800 bg-green-50 border border-green-200 rounded-md p-3">
          Lead won — a draft project was created automatically.{" "}
          <Link href="/projects" className="underline">
            Open projects
          </Link>{" "}
          to set its site address and get it moving.
        </p>
      )}

      {nextStatuses.length > 0 && (
        <div className="flex gap-2">
          {nextStatuses.map((next) => (
            <Button
              key={next}
              variant={next === "lost" ? "outline" : undefined}
              disabled={submitting}
              onClick={() =>
                patchLead({ status: next }, (updated) => {
                  if (updated.status === "won") setWonBanner(true);
                })
              }
            >
              Mark {labelFor(next).toLowerCase()}
            </Button>
          ))}
        </div>
      )}

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      <div>
        <Button variant="outline" size="sm" onClick={() => setEditing((v) => !v)}>
          {editing ? "Close edit" : "Edit details"}
        </Button>
      </div>
      {editing && (
        <LeadForm
          initial={{
            contact_name: lead.contact_name,
            project_name: lead.project_name,
            email: lead.email,
            phone: lead.phone ?? "",
            project_type: lead.project_type,
            estimated_value: lead.estimated_value ?? "",
            notes: lead.notes ?? "",
          }}
          submitLabel="Save changes"
          submitting={submitting}
          error={null}
          onSubmit={(values: LeadFormValues) => {
            patchLead(leadPayload(values), () => setEditing(false));
          }}
        />
      )}

      <CommunicationLog leadId={lead.id} />
    </main>
  );
}
