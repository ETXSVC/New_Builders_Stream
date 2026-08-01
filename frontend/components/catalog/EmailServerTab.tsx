"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useLatestOnly } from "@/lib/use-latest-only";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface EmailSettings {
  host: string;
  port: number;
  username: string | null;
  from_address: string;
  starttls: boolean;
  enabled: boolean;
  has_password: boolean;
  verified_at: string | null;
}

const EMPTY = {
  host: "",
  port: "587",
  username: "",
  password: "",
  from_address: "",
  starttls: true,
  enabled: true,
};

/**
 * Send through your own mail server.
 *
 * The password is write-only on the way in and absent on the way back, so
 * this form shows "a password is stored" rather than dots that pretend to
 * be one: leaving the field blank keeps it, and there is an explicit
 * control for removing it. A form that could only ever overwrite a
 * credential is one people avoid touching.
 *
 * Saving is not proof. `verified_at` comes back null on every save, and
 * only the Test button — which sends a real message to the signed-in
 * admin — sets it. A screen that says "saved" next to a mail server nobody
 * has ever reached is how a company finds out at the worst moment.
 */
export function EmailServerTab() {
  const { accessToken } = useAuth();
  const [settings, setSettings] = React.useState<EmailSettings | null>(null);
  const [loaded, setLoaded] = React.useState(false);
  const [form, setForm] = React.useState(EMPTY);
  const [clearPassword, setClearPassword] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);

  const beginLoad = useLatestOnly();
  const load = React.useCallback(async () => {
    if (!accessToken) return;
    const isCurrent = beginLoad();
    try {
      const response = await fetch("/api/companies/email-settings", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!isCurrent()) return;
      if (!response.ok) {
        setError(data?.detail ?? "Failed to load the mail server settings");
        return;
      }
      setLoaded(true);
      setSettings(data);
      if (data) {
        setForm({
          host: data.host,
          port: String(data.port),
          username: data.username ?? "",
          password: "",
          from_address: data.from_address,
          starttls: data.starttls,
          enabled: data.enabled,
        });
      }
    } catch {
      if (isCurrent()) {
        setError("Unable to reach the server. Check your connection and try again.");
      }
    }
  }, [accessToken, beginLoad]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !accessToken) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch("/api/companies/email-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          host: form.host.trim(),
          port: Number(form.port),
          username: form.username.trim() || null,
          // Three states, not two: a typed value sets it, an explicit
          // "remove" clears it, and neither leaves the stored one alone.
          password: form.password ? form.password : clearPassword ? "" : null,
          from_address: form.from_address.trim(),
          starttls: form.starttls,
          enabled: form.enabled,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data?.detail ?? "Failed to save the mail server settings");
        return;
      }
      setSettings(data);
      setForm((previous) => ({ ...previous, password: "" }));
      setClearPassword(false);
      setNotice("Saved. Send a test message to prove it works.");
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  async function sendTest() {
    if (busy || !accessToken) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch("/api/companies/email-settings/test", {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data?.detail ?? "Could not send the test message");
        return;
      }
      // ok=false is a normal answer here — a refused login is what testing
      // a configuration is for — so the relay's own words go in the banner
      // rather than a generic failure.
      if (data.ok) {
        setNotice(data.detail);
        await load();
      } else {
        setError(data.detail);
      }
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (busy || !accessToken) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch("/api/companies/email-settings", {
        method: "DELETE",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        setError(data?.detail ?? "Could not remove the mail server settings");
        return;
      }
      setSettings(null);
      setForm(EMPTY);
      setNotice("Removed. Mail now goes out through the platform's own server.");
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  if (!loaded) return <p className="text-sm text-slate-500">Loading…</p>;

  return (
    <div className="flex max-w-xl flex-col gap-4">
      <p className="text-sm text-slate-500">
        By default your mail goes out through Builders Stream&apos;s own server. Point this at
        your own to send from your domain — which needs SPF and DKIM records published for it,
        or the mail is likely to be treated as spam.
      </p>

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {notice && !error && (
        <p role="status" className="text-sm text-green-700">
          {notice}
        </p>
      )}

      {settings && (
        <p className="text-xs text-slate-500">
          {settings.verified_at
            ? `Last verified ${new Date(settings.verified_at).toLocaleString()}.`
            : "Never verified — saving does not prove a server works. Send a test message."}
        </p>
      )}

      <form onSubmit={save} className="flex flex-col gap-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mail-host">Server hostname</Label>
            <Input
              id="mail-host"
              value={form.host}
              maxLength={255}
              placeholder="smtp.yourprovider.com"
              disabled={busy}
              onChange={(e) => setForm((f) => ({ ...f, host: e.target.value }))}
              required
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mail-port">Port</Label>
            <Input
              id="mail-port"
              type="number"
              min={1}
              max={65535}
              value={form.port}
              disabled={busy}
              onChange={(e) => setForm((f) => ({ ...f, port: e.target.value }))}
              required
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mail-username">Username</Label>
            <Input
              id="mail-username"
              value={form.username}
              maxLength={255}
              disabled={busy}
              onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mail-from">Send from</Label>
            <Input
              id="mail-from"
              type="email"
              value={form.from_address}
              maxLength={255}
              placeholder="no-reply@yourdomain.com"
              disabled={busy}
              onChange={(e) => setForm((f) => ({ ...f, from_address: e.target.value }))}
              required
            />
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="mail-password">Password</Label>
          <Input
            id="mail-password"
            type="password"
            autoComplete="new-password"
            value={form.password}
            disabled={busy || clearPassword}
            placeholder={settings?.has_password ? "Stored — leave blank to keep it" : ""}
            onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
          />
          {settings?.has_password && (
            <label className="flex items-center gap-2 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={clearPassword}
                disabled={busy}
                onChange={(e) => {
                  setClearPassword(e.target.checked);
                  if (e.target.checked) setForm((f) => ({ ...f, password: "" }));
                }}
              />
              Remove the stored password (for a relay that needs none)
            </label>
          )}
        </div>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.starttls}
            disabled={busy}
            onChange={(e) => setForm((f) => ({ ...f, starttls: e.target.checked }))}
          />
          Use STARTTLS
        </label>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.enabled}
            disabled={busy}
            onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
          />
          Send through this server. Turn it off to fall back to Builders Stream&apos;s while
          keeping these settings.
        </label>

        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" disabled={busy}>
            {busy ? "Working…" : "Save"}
          </Button>
          {settings && (
            <>
              <Button type="button" variant="outline" disabled={busy} onClick={() => void sendTest()}>
                Send test message
              </Button>
              <ConfirmButton
                type="button"
                variant="ghost"
                confirmMessage="Remove these settings and go back to Builders Stream's server?"
                confirmLabel="Remove"
                disabled={busy}
                onConfirm={() => void remove()}
              >
                Remove
              </ConfirmButton>
            </>
          )}
        </div>
      </form>
    </div>
  );
}
