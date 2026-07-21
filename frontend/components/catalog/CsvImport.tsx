"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { parseCatalogCsv, serializeCatalogCsv, CsvParseError, CatalogCsvRow } from "@/lib/csv";

interface ImportResultEntry {
  index: number;
  status: string;
  detail: string | null;
}

export function CsvImport({
  currentItems,
  onImported,
}: {
  currentItems: CatalogCsvRow[];
  onImported: () => void;
}) {
  const { accessToken } = useAuth();
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);
  const [preview, setPreview] = React.useState<CatalogCsvRow[] | null>(null);
  const [parseError, setParseError] = React.useState<string | null>(null);
  const [results, setResults] = React.useState<ImportResultEntry[] | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setParseError(null);
    setResults(null);
    try {
      const text = await file.text();
      const rows = parseCatalogCsv(text);
      setPreview(rows);
    } catch (err) {
      setPreview(null);
      setParseError(err instanceof CsvParseError ? err.message : "Unable to read file");
    }
  }

  async function handleImport() {
    if (!preview || submitting || !accessToken) return;
    setSubmitting(true);
    try {
      const response = await fetch("/api/catalog/items/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          items: preview.map((r) => ({
            category: r.category,
            name: r.name,
            unit: r.unit,
            unit_rate: r.unit_rate,
          })),
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setParseError(data.detail ?? "Import failed");
        return;
      }
      setResults(data.results);
      setPreview(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      onImported();
    } catch {
      setParseError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  function handleExport() {
    const csv = serializeCatalogCsv(currentItems);
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "catalog-export.csv";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return (
    <div className="flex flex-col gap-2 border border-slate-200 rounded-md p-3">
      <div className="flex items-center gap-2">
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,text/csv"
          aria-label="Import CSV"
          onChange={handleFileChange}
          className="text-sm"
        />
        <Button type="button" variant="outline" size="sm" onClick={handleExport}>
          Export CSV
        </Button>
      </div>
      {parseError && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {parseError}
        </p>
      )}
      {preview && (
        <div className="flex flex-col gap-2">
          <p className="text-sm">{preview.length} row(s) ready to import.</p>
          <Button type="button" size="sm" onClick={handleImport} disabled={submitting}>
            {submitting ? "Importing…" : "Import"}
          </Button>
        </div>
      )}
      {results && (
        <ul className="text-sm">
          {results.map((r) => (
            <li key={r.index} className={r.status === "error" ? "text-red-600" : "text-green-700"}>
              Row {r.index + 1}: {r.status}
              {r.detail ? ` — ${r.detail}` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
