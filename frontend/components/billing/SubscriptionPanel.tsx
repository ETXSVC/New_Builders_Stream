"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDate } from "@/lib/format";

interface Subscription {
  tier: string;
  status: string;
  included_seats: number;
  current_period_end: string | null;
}

export function SubscriptionPanel() {
  const { accessToken, role } = useAuth();
  const [subscription, setSubscription] = React.useState<Subscription | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [opening, setOpening] = React.useState(false);

  React.useEffect(() => {
    if (!accessToken) return;
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch("/api/subscriptions/me", {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (cancelled) return;
        if (!response.ok) {
          setError(data.detail ?? "Failed to load subscription");
          return;
        }
        setSubscription(data);
      } catch {
        if (!cancelled) setError("Unable to reach the server.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [accessToken]);

  async function openPortal() {
    if (opening || !accessToken) return;
    setOpening(true);
    setError(null);
    try {
      const response = await fetch("/api/subscriptions/portal-session", {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to open the billing portal");
        return;
      }
      window.location.assign(data.url);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setOpening(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 max-w-md">
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {subscription && (
        <dl className="flex flex-col gap-3 rounded-lg border border-slate-200 p-4 text-sm">
          <div className="flex justify-between">
            <dt className="text-slate-600">Plan</dt>
            <dd className="font-medium capitalize">{subscription.tier}</dd>
          </div>
          <div className="flex justify-between items-center">
            <dt className="text-slate-600">Status</dt>
            <dd>
              <StatusBadge status={subscription.status} />
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-600">Included seats</dt>
            <dd>{subscription.included_seats}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-600">Current period ends</dt>
            <dd>{subscription.current_period_end ? formatDate(subscription.current_period_end) : "—"}</dd>
          </div>
        </dl>
      )}
      {/* Portal session is admin-only on the backend (_PORTAL_ROLES) —
          mirrored here so an accountant isn't offered a button that 403s. */}
      {role === "admin" && (
        <Button onClick={openPortal} disabled={opening}>
          {opening ? "Opening…" : "Manage billing"}
        </Button>
      )}
    </div>
  );
}
