"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";

export function PdfPanel({ estimateId, pdfStatus, canExport }: { estimateId: string; pdfStatus: string; canExport: boolean }) {
  const { accessToken } = useAuth();
  const [status, setStatus] = React.useState(pdfStatus);
  const [viewerUrl, setViewerUrl] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [requesting, setRequesting] = React.useState(false);

  React.useEffect(() => {
    setStatus(pdfStatus);
  }, [pdfStatus]);

  React.useEffect(() => {
    if (status !== "pending" || !accessToken) return;
    const interval = setInterval(async () => {
      const response = await fetch(`/api/estimates/${estimateId}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (response.ok) setStatus(data.pdf_status);
    }, 3000);
    return () => clearInterval(interval);
  }, [status, accessToken, estimateId]);

  const loadViewer = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/estimates/${estimateId}/pdf`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) return;
      const blob = await response.blob();
      setViewerUrl(URL.createObjectURL(blob));
    } catch {
      // Viewer stays unset — the Download button below still works via its
      // own fetch and is the fallback path if inline preview fails.
    }
  }, [accessToken, estimateId]);

  React.useEffect(() => {
    if (status === "ready") void Promise.resolve().then(() => loadViewer());
  }, [status, loadViewer]);

  React.useEffect(() => {
    return () => {
      if (viewerUrl) URL.revokeObjectURL(viewerUrl);
    };
  }, [viewerUrl]);

  async function handleExport() {
    if (requesting || !accessToken) return;
    setError(null);
    setRequesting(true);
    try {
      const response = await fetch(`/api/estimates/${estimateId}/export`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to export PDF");
        return;
      }
      setStatus(data.pdf_status);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setRequesting(false);
    }
  }

  async function handleDownload() {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/estimates/${estimateId}/pdf`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) {
        setError("Download failed");
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `estimate-${estimateId}.pdf`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }

  return (
    <div className="flex flex-col gap-3 border border-slate-200 rounded-md p-4">
      <p className="text-sm font-medium">PDF export</p>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {status === "not_requested" && canExport && (
        <Button type="button" onClick={handleExport} disabled={requesting}>
          Generate PDF
        </Button>
      )}
      {status === "pending" && <p className="text-sm text-slate-500">Generating… this can take a moment.</p>}
      {status === "failed" && (
        <>
          <p className="text-sm text-red-600">PDF generation failed.</p>
          {canExport && (
            <Button type="button" onClick={handleExport} disabled={requesting}>
              Retry export
            </Button>
          )}
        </>
      )}
      {status === "ready" && (
        <div className="flex flex-col gap-2">
          {viewerUrl && (
            <iframe src={viewerUrl} title="Estimate PDF" className="w-full h-96 border border-slate-200 rounded" />
          )}
          <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={handleDownload}>
              Download
            </Button>
            {canExport && (
              <Button type="button" variant="outline" onClick={handleExport} disabled={requesting}>
                Regenerate
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
