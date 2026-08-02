"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface Settings {
  deposit_percentage: string | null;
  tax_rate: string | null;
  effective_deposit_percentage: string;
  effective_tax_rate: string;
}

/**
 * Deposit percentage and tax rate, entered as PERCENTAGES.
 *
 * The API stores fractions (0.08875), because every consumer multiplies
 * directly. People do not think in fractions about tax, so the conversion
 * happens here, at the one place a human types a number — rather than in
 * the API, where "is this 8.875 or 0.08875" would then be ambiguous at
 * every call site.
 *
 * An empty field means "inherit" and sends null: a branch with nothing set
 * follows its head office, and a company with nothing set gets the
 * platform default. That is why the helper text names the effective value
 * whenever the field itself is blank — otherwise the screen would say
 * "not set" while invoices were being raised at 20%.
 */
export function FinancialSettingsPanel() {
  const { accessToken, role } = useAuth();
  const [settings, setSettings] = React.useState<Settings | null>(null);
  const [deposit, setDeposit] = React.useState("");
  const [tax, setTax] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [saved, setSaved] = React.useState(false);

  const canEdit = role === "admin" || role === "accountant";

  const apply = React.useCallback((data: Settings) => {
    setSettings(data);
    setDeposit(data.deposit_percentage === null ? "" : toPercent(data.deposit_percentage));
    setTax(data.tax_rate === null ? "" : toPercent(data.tax_rate));
  }, []);

  const load = React.useCallback(async () => {
    if (!accessToken || !canEdit) return;
    try {
      const response = await fetch("/api/companies/financial-settings", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) return;
      apply(await response.json());
    } catch {
      // Leave the panel blank rather than banner an error — nothing else
      // on this page depends on it.
    }
  }, [accessToken, apply, canEdit]);

  React.useEffect(() => {
    // Deferred so no setState runs synchronously inside the effect body.
    void Promise.resolve().then(() => load());
  }, [load]);

  async function handleSave(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const response = await fetch("/api/companies/financial-settings", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({
          deposit_percentage: toFraction(deposit),
          tax_rate: toFraction(tax),
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(typeof data.detail === "string" ? data.detail : "Could not save");
        return;
      }
      apply(data);
      setSaved(true);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSaving(false);
    }
  }

  if (!canEdit) return null;

  return (
    <section className="flex flex-col gap-3 max-w-md">
      <h2 className="text-sm font-medium text-slate-700">Financial defaults</h2>
      <p className="text-xs text-slate-500">
        Used for the deposit invoice raised when an estimate is approved, and for the
        estimated tax figure on the profitability report. Leave a field empty to use
        your head office&rsquo;s setting.
      </p>

      <form onSubmit={handleSave} className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor="deposit-percentage">Deposit percentage</Label>
          <div className="flex items-center gap-2">
            <Input
              id="deposit-percentage"
              inputMode="decimal"
              value={deposit}
              onChange={(event) => setDeposit(event.target.value)}
              placeholder="10"
              className="max-w-[8rem]"
            />
            <span className="text-sm text-slate-600">%</span>
          </div>
          {settings && deposit === "" && (
            <p className="text-xs text-slate-500">
              Inheriting {toPercent(settings.effective_deposit_percentage)}%
            </p>
          )}
        </div>

        <div className="flex flex-col gap-1">
          <Label htmlFor="tax-rate">Tax rate</Label>
          <div className="flex items-center gap-2">
            <Input
              id="tax-rate"
              inputMode="decimal"
              value={tax}
              onChange={(event) => setTax(event.target.value)}
              placeholder="8.875"
              className="max-w-[8rem]"
            />
            <span className="text-sm text-slate-600">%</span>
          </div>
          {settings && tax === "" && (
            <p className="text-xs text-slate-500">
              Inheriting {toPercent(settings.effective_tax_rate)}%
            </p>
          )}
        </div>

        {error && (
          <p role="alert" className="text-sm text-red-700">
            {error}
          </p>
        )}
        {saved && !error && <p className="text-sm text-emerald-700">Saved.</p>}

        <div>
          <Button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </section>
  );
}

function toPercent(fraction: string): string {
  const numeric = Number(fraction);
  if (Number.isNaN(numeric)) return fraction;
  // Rounded to 3 decimals: the API stores 5 decimal places as a fraction,
  // which is exactly 3 as a percentage (0.08875 -> 8.875). More would be
  // float noise, fewer would lose a real rate.
  return String(Math.round(numeric * 100 * 1000) / 1000);
}

function toFraction(percent: string): string | null {
  const trimmed = percent.trim();
  // Empty means "inherit", which the API expresses as null.
  if (trimmed === "") return null;
  const numeric = Number(trimmed);
  if (Number.isNaN(numeric)) return trimmed; // let the API reject it by name
  return (Math.round((numeric / 100) * 100000) / 100000).toString();
}
