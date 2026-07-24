"use client";

import * as React from "react";
import { useParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { PlanUpgradeNotice, isPlanGateError } from "@/components/billing/PlanUpgradeNotice";
import { formatDate } from "@/lib/format";

interface Subcontractor {
  id: string;
  name: string;
  trade: string | null;
  contact_email: string | null;
}

interface ComplianceDocument {
  id: string;
  doc_type: string;
  expires_on: string;
  created_at: string;
}

const DOC_TYPES = [
  { value: "insurance_certificate", label: "Insurance certificate" },
  { value: "license", label: "License" },
] as const;

const DOC_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  DOC_TYPES.map((d) => [d.value, d.label])
);

// Mirrors the backend dashboard's vocabulary ("expired" / "expiring_soon"
// with a 30-day window, app/routers/compliance.py) plus "valid" for
// documents the dashboard wouldn't list at all.
function expiryStatus(expiresOn: string): string {
  const expiry = new Date(expiresOn);
  const now = new Date();
  if (expiry < now) return "expired";
  const soon = new Date();
  soon.setDate(soon.getDate() + 30);
  return expiry < soon ? "expiring_soon" : "valid";
}

export default function SubcontractorDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { accessToken, role } = useAuth();
  const [subcontractor, setSubcontractor] = React.useState<Subcontractor | null>(null);
  const [documents, setDocuments] = React.useState<ComplianceDocument[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [planGate, setPlanGate] = React.useState<string | null>(null);

  const [docType, setDocType] = React.useState<string>(DOC_TYPES[0].value);
  const [expiresOn, setExpiresOn] = React.useState("");
  const [file, setFile] = React.useState<File | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = React.useState(false);

  // Uploads are admin-only on the backend.
  const canUpload = role === "admin";

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const [subResponse, docsResponse] = await Promise.all([
        fetch(`/api/subcontractors/${id}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        }),
        fetch(`/api/subcontractors/${id}/compliance-documents`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        }),
      ]);
      const subData = await subResponse.json();
      if (!subResponse.ok) {
        setError(subData.detail ?? "Failed to load subcontractor");
        return;
      }
      setSubcontractor(subData);
      const docsData = await docsResponse.json();
      if (docsResponse.ok) setDocuments(docsData.items ?? []);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, id]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (uploading || !accessToken || !file) return;
    setUploading(true);
    setError(null);
    setPlanGate(null);
    try {
      const formData = new FormData();
      formData.set("doc_type", docType);
      formData.set("expires_on", expiresOn);
      formData.set("file", file);
      const response = await fetch(`/api/subcontractors/${id}/compliance-documents`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
        body: formData,
      });
      const data = await response.json();
      if (!response.ok) {
        if (isPlanGateError(response.status, data.detail)) {
          setPlanGate(data.detail);
        } else {
          setError(
            typeof data.detail === "string" ? data.detail : "Failed to upload compliance document"
          );
        }
        return;
      }
      setExpiresOn("");
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setUploading(false);
    }
  }

  if (!subcontractor) {
    return (
      <main className="p-6">
        {error ? (
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        ) : (
          <p className="text-sm text-slate-500">Loading…</p>
        )}
      </main>
    );
  }

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <div>
        <h1 className="text-xl font-semibold">{subcontractor.name}</h1>
        <p className="text-sm text-slate-600">
          {subcontractor.trade ?? "No trade specified"}
          {subcontractor.contact_email ? ` · ${subcontractor.contact_email}` : ""}
        </p>
      </div>

      {planGate && <PlanUpgradeNotice detail={planGate} />}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold">Compliance documents</h2>
        {documents.length === 0 && (
          <p className="text-sm text-slate-600">No compliance documents on file.</p>
        )}
        <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
          {documents.map((doc) => (
            <li key={doc.id} className="flex items-center gap-4 px-4 py-3 text-sm">
              <span className="flex-1 font-medium">
                {DOC_TYPE_LABELS[doc.doc_type] ?? doc.doc_type}
              </span>
              <span className="text-slate-500">expires {formatDate(doc.expires_on)}</span>
              <StatusBadge status={expiryStatus(doc.expires_on)} />
            </li>
          ))}
        </ul>

        {canUpload && (
          <form onSubmit={handleUpload} className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="docType">Document type</Label>
              <Select
                id="docType"
                value={docType}
                onChange={(e) => setDocType(e.target.value)}
                disabled={uploading}
                className="w-52"
              >
                {DOC_TYPES.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="docExpires">Expires on</Label>
              <Input
                id="docExpires"
                type="date"
                value={expiresOn}
                onChange={(e) => setExpiresOn(e.target.value)}
                disabled={uploading}
                required
                className="w-40"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="docFile">File</Label>
              <Input
                id="docFile"
                type="file"
                ref={fileInputRef}
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                disabled={uploading}
                required
              />
            </div>
            <Button type="submit" disabled={uploading || !file}>
              {uploading ? "Uploading…" : "Upload"}
            </Button>
          </form>
        )}
      </section>
    </main>
  );
}
