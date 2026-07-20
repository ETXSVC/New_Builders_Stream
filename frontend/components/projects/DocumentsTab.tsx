"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";

interface Doc {
  id: string;
  file_name: string;
  version: number;
  created_at: string;
}

export function DocumentsTab({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [docs, setDocs] = React.useState<Doc[]>([]);
  const [file, setFile] = React.useState<File | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);

  const canUpload = role === "admin" || role === "project_manager";

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      // The backend pages at 25/entry ascending by created_at, so a
      // just-uploaded document lands on the LAST page — follow next_cursor
      // to exhaustion so it (and everything else) is always visible. List
      // sizes here are small enough that a few sequential requests are fine.
      const all: Doc[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/projects/${projectId}/documents?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load documents");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setDocs(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    // Deferred to a promise callback so no setState in loadAll's call path
    // runs synchronously inside the effect (react-hooks/set-state-in-effect).
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken || !file) return;
    setError(null);
    setSubmitting(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("file_name", file.name);
      const response = await fetch(`/api/projects/${projectId}/documents`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
        body: formData,
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to upload document");
        return;
      }
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await loadAll();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDownload(doc: Doc) {
    if (!accessToken) return;
    setError(null);
    try {
      // fetch-with-bearer, then a programmatic download: a plain <a href>
      // navigation would carry no Authorization header (the access token
      // lives only in memory — Foundation's BFF session design).
      const response = await fetch(`/api/projects/${projectId}/documents/${doc.id}/download`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) {
        let detail = "Download failed";
        try {
          detail = (await response.json()).detail ?? detail;
        } catch {}
        setError(detail);
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = doc.file_name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      // Deferred: revoking synchronously can cancel the download in
      // browsers that resolve the URL after the click event returns.
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }

  return (
    <section className="flex flex-col gap-4">
      {canUpload && (
        <form onSubmit={handleUpload} className="flex items-center gap-2">
          <input
            ref={fileInputRef}
            aria-label="Choose file"
            type="file"
            className="text-sm"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            disabled={submitting}
          />
          <Button type="submit" disabled={submitting || !file}>
            Upload
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {docs.length === 0 && <p className="text-sm text-slate-600">No documents yet.</p>}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {docs.map((doc) => (
          <li key={doc.id} className="flex items-center gap-4 px-4 py-3 text-sm">
            <span className="flex-1 font-medium">{doc.file_name}</span>
            <span className="text-slate-500">v{doc.version}</span>
            <span className="text-slate-500">{new Date(doc.created_at).toLocaleDateString()}</span>
            <Button variant="outline" size="sm" onClick={() => handleDownload(doc)}>
              Download
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}
