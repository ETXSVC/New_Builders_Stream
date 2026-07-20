"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { EMPTY_LEAD_FORM, LeadForm, leadPayload, LeadFormValues } from "@/components/leads/LeadForm";

export default function NewLeadPage() {
  const router = useRouter();
  const { accessToken } = useAuth();
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function handleSubmit(values: LeadFormValues) {
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/leads", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify(leadPayload(values)),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create lead");
        return;
      }
      router.push(`/leads/${data.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="p-6 flex flex-col gap-4">
      <h1 className="text-xl font-semibold">New lead</h1>
      <LeadForm initial={EMPTY_LEAD_FORM} submitLabel="Create lead" onSubmit={handleSubmit} submitting={submitting} error={error} />
    </main>
  );
}
