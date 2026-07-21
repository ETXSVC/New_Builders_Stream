"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

interface Branding {
  logo_storage_path: string | null;
  accent_color: string;
  footer_text: string;
}

export function BrandingTab() {
  const { accessToken } = useAuth();
  const [branding, setBranding] = React.useState<Branding | null>(null);
  const [accentColor, setAccentColor] = React.useState("#1e293b");
  const [footerText, setFooterText] = React.useState("");
  const [logoFile, setLogoFile] = React.useState<File | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch("/api/companies/branding", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load branding");
        return;
      }
      setBranding(data);
      setAccentColor(data.accent_color);
      setFooterText(data.footer_text);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSaved(false);
    setSubmitting(true);
    try {
      const response = await fetch("/api/companies/branding", {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ accent_color: accentColor, footer_text: footerText }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to save branding");
        return;
      }

      if (logoFile) {
        const formData = new FormData();
        formData.append("file", logoFile);
        const logoResponse = await fetch("/api/companies/branding/logo", {
          method: "POST",
          headers: { Authorization: `Bearer ${accessToken}` },
          body: formData,
        });
        const logoData = await logoResponse.json();
        if (!logoResponse.ok) {
          setError(logoData.detail ?? "Failed to upload logo");
          return;
        }
      }

      setSaved(true);
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (!branding) {
    return <p className="text-sm text-slate-500">Loading…</p>;
  }

  return (
    <form onSubmit={handleSave} className="flex flex-col gap-4 max-w-md">
      <p className="text-sm text-slate-500">Applies to future PDF exports — already-generated PDFs don&apos;t change.</p>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="branding-logo">Logo (PNG or JPEG, up to 2 MB)</Label>
        {branding.logo_storage_path && <p className="text-xs text-slate-500">Current logo is set.</p>}
        <input
          id="branding-logo"
          type="file"
          accept="image/png,image/jpeg"
          onChange={(e) => setLogoFile(e.target.files?.[0] ?? null)}
          disabled={submitting}
          className="text-sm"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="branding-accent">Accent color</Label>
        <Input
          id="branding-accent"
          type="text"
          pattern="^#[0-9a-fA-F]{6}$"
          value={accentColor}
          onChange={(e) => setAccentColor(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="branding-footer">Footer / terms text</Label>
        <Textarea
          id="branding-footer"
          value={footerText}
          onChange={(e) => setFooterText(e.target.value)}
          disabled={submitting}
        />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {saved && <p className="text-sm text-green-700">Saved.</p>}
      <Button type="submit" disabled={submitting}>
        {submitting ? "Saving…" : "Save"}
      </Button>
    </form>
  );
}
