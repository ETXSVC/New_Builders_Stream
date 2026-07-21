"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { TypedSignature } from "./TypedSignature";

// Shared approve/reject panel used by both the estimate detail page (sent
// state) and the client's inline change-order card. `approveUrl`/`rejectUrl`
// are the BFF routes to POST to — the two callers point this at different
// endpoints but the interaction is identical.
export function SigningPanel({
  approveUrl,
  rejectUrl,
  accessToken,
  onDone,
}: {
  approveUrl: string;
  rejectUrl: string;
  accessToken: string;
  onDone: () => void;
}) {
  const [mode, setMode] = React.useState<"choose" | "reject">("choose");
  const [reason, setReason] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function handleApprove({
    signerName,
    signerEmail,
    artifact,
  }: {
    signerName: string;
    signerEmail: string;
    artifact: Blob;
  }) {
    setError(null);
    setSubmitting(true);
    try {
      const formData = new FormData();
      formData.append("signer_name", signerName);
      formData.append("signer_email", signerEmail);
      formData.append("signature_artifact", artifact, "signature.png");
      const response = await fetch(approveUrl, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
        body: formData,
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to approve");
        return;
      }
      onDone();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleReject(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !reason.trim()) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(rejectUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ reason }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to reject");
        return;
      }
      onDone();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-3 border border-slate-200 rounded-md p-4">
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {mode === "choose" && (
        <>
          <TypedSignature onSign={handleApprove} submitting={submitting} />
          <button
            type="button"
            onClick={() => setMode("reject")}
            disabled={submitting}
            className="text-sm text-slate-500 hover:underline self-start"
          >
            Reject instead
          </button>
        </>
      )}
      {mode === "reject" && (
        <form onSubmit={handleReject} className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="reject-reason">Reason for rejecting</Label>
            <Textarea
              id="reject-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={submitting}
              required
            />
          </div>
          <div className="flex gap-2">
            <Button type="submit" variant="outline" disabled={submitting || !reason.trim()}>
              {submitting ? "Submitting…" : "Reject"}
            </Button>
            <button
              type="button"
              onClick={() => setMode("choose")}
              disabled={submitting}
              className="text-sm text-slate-500 hover:underline"
            >
              Back
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
