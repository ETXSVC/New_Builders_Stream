"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function NewProjectPage() {
  const router = useRouter();
  const { accessToken } = useAuth();
  const [name, setName] = React.useState("");
  const [siteAddress, setSiteAddress] = React.useState("");
  const [startDate, setStartDate] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          name,
          site_address: siteAddress,
          projected_start_date: startDate || null,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create project");
        return;
      }
      router.push(`/projects/${data.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="p-6 flex flex-col gap-4">
      <h1 className="text-xl font-semibold">New project</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-md">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="name">Project name</Label>
          <Input id="name" value={name} onChange={(e) => setName(e.target.value)} disabled={submitting} required />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="site_address">Site address</Label>
          <Input id="site_address" value={siteAddress} onChange={(e) => setSiteAddress(e.target.value)} disabled={submitting} required />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="start_date">Projected start date (optional)</Label>
          <Input id="start_date" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} disabled={submitting} />
        </div>
        {error && (
          <p role="alert" aria-live="assertive" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <Button type="submit" disabled={submitting}>
          Create project
        </Button>
      </form>
    </main>
  );
}
