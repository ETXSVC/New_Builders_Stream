"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";

interface Membership {
  company_id: string;
  company_name: string;
  role: string;
  parent_id: string | null;
  is_active: boolean;
}

/**
 * Switch which company the session is acting as.
 *
 * Renders NOTHING when the user belongs to one company, which is every user
 * who has not been invited into a second one — a control with a single
 * option is noise, and until migration 0031 a second membership could not
 * exist at all.
 *
 * Switching replaces the whole session: the backend re-mints the access
 * token so it names the new company (POST /auth/switch-company), and the
 * rotated refresh token lands back in the httpOnly cookie. So this calls
 * `setSession` with the new token rather than poking at a separate "active
 * tenant" value — there is exactly one source of truth for which company is
 * active, and it is the token every request already carries.
 *
 * `router.refresh()` afterwards because every page on screen was rendered
 * for the previous company. Without it the switch would appear to work — the
 * header would update — while the list below it still showed the old
 * company's rows until something happened to refetch.
 */
export function CompanySwitcher() {
  const { accessToken, setSession } = useAuth();
  const router = useRouter();
  const [memberships, setMemberships] = React.useState<Membership[]>([]);
  const [switching, setSwitching] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch("/api/companies/memberships", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) return;
      const data = await response.json();
      setMemberships(data.memberships ?? []);
    } catch {
      // A switcher that fails to load is not worth an error banner across
      // the top of every page — the app is entirely usable in the company
      // the session is already in.
    }
  }, [accessToken]);

  React.useEffect(() => {
    // Deferred so no setState runs synchronously inside the effect body —
    // same rule as lib/use-cursor-list.ts.
    void Promise.resolve().then(() => load());
  }, [load]);

  async function handleChange(event: React.ChangeEvent<HTMLSelectElement>) {
    const companyId = event.target.value;
    setSwitching(true);
    setError(null);
    try {
      const response = await fetch("/api/auth/switch-company", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company_id: companyId }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Could not switch company");
        return;
      }
      // No explicit reload: setSession hands out a new access token, which
      // changes `load`'s identity and re-runs the effect above. Calling it
      // here as well fetched the list twice per switch — visible in the
      // backend log as two /companies/memberships hits.
      setSession(data.access_token, data.mfa_enrollment_required, data.role);
      router.refresh();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSwitching(false);
    }
  }

  if (memberships.length < 2) return null;

  const active = memberships.find((m) => m.is_active)?.company_id ?? "";

  return (
    <div className="flex items-center gap-2">
      <label htmlFor="company-switcher" className="sr-only">
        Active company
      </label>
      <select
        id="company-switcher"
        value={active}
        onChange={handleChange}
        disabled={switching}
        className="text-sm border border-slate-300 rounded-md px-2 py-1 bg-white disabled:opacity-60"
      >
        {memberships.map((membership) => (
          <option key={membership.company_id} value={membership.company_id}>
            {/* The role is part of the identity here: the same person is an
                admin in one company and a field crew member in another, and
                which one they are about to become changes what the app will
                let them do. */}
            {membership.company_name} ({membership.role.replace(/_/g, " ")})
          </option>
        ))}
      </select>
      {error && (
        <span role="alert" className="text-xs text-red-700">
          {error}
        </span>
      )}
    </div>
  );
}
