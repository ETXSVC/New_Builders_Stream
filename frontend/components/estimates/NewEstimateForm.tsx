"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

interface MarkupProfileOption {
  id: string;
  name: string;
}

export function NewEstimateForm({
  projectId,
  leadId,
}: {
  projectId?: string;
  leadId?: string;
}) {
  const router = useRouter();
  const { accessToken } = useAuth();
  const [profiles, setProfiles] = React.useState<MarkupProfileOption[]>([]);
  const [markupProfileId, setMarkupProfileId] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const loadProfiles = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch("/api/markup-profiles", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (response.ok) setProfiles(data.items);
    } catch {
      // Non-blocking — the Select just stays empty if this fails.
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadProfiles());
  }, [loadProfiles]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken || !markupProfileId) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/estimates", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          project_id: projectId ?? null,
          lead_id: leadId ?? null,
          markup_profile_id: markupProfileId,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create estimate");
        return;
      }
      router.push(`/estimates/${data.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-sm">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="new-estimate-markup">Markup profile</Label>
        <Select
          id="new-estimate-markup"
          value={markupProfileId}
          onChange={(e) => setMarkupProfileId(e.target.value)}
          disabled={submitting}
          required
        >
          <option value="">Select…</option>
          {profiles.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </Select>
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <Button type="submit" disabled={submitting || !markupProfileId}>
        {submitting ? "Creating…" : "Create estimate"}
      </Button>
    </form>
  );
}
