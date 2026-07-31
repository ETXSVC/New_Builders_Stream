"use client";

import * as React from "react";
import type { components } from "@/lib/api/types";
import { useAuth } from "@/contexts/AuthContext";
import { useLatestOnly } from "@/lib/use-latest-only";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MemberPhoto } from "@/components/team/MemberPhoto";
import { PhonesEditor, filledPhones } from "@/components/team/PhonesEditor";

type TeamMember = components["schemas"]["TeamMemberResponse"];
type PhoneEntry = components["schemas"]["PhoneEntry"];

const FIELDS = [
  { key: "first_name", label: "First name", max: 100 },
  { key: "last_name", label: "Last name", max: 100 },
  { key: "address_line1", label: "Address line 1", max: 255 },
  { key: "address_line2", label: "Address line 2", max: 255 },
  { key: "city", label: "City", max: 100 },
  { key: "state", label: "State", max: 100 },
  { key: "postal_code", label: "Postal code", max: 20 },
] as const;

type FieldKey = (typeof FIELDS)[number]["key"];
type Draft = Record<FieldKey, string>;

const EMPTY: Draft = {
  first_name: "",
  last_name: "",
  address_line1: "",
  address_line2: "",
  city: "",
  state: "",
  postal_code: "",
};

function draftFrom(profile: TeamMember): Draft {
  return {
    first_name: profile.first_name ?? "",
    last_name: profile.last_name ?? "",
    address_line1: profile.address_line1 ?? "",
    address_line2: profile.address_line2 ?? "",
    city: profile.city ?? "",
    state: profile.state ?? "",
    postal_code: profile.postal_code ?? "",
  };
}

/**
 * Your own entry in the company directory.
 *
 * On the account page rather than in the directory itself, because the
 * directory is admin + project_manager and this has to work for field crew,
 * who cannot open it at all. It talks to `/api/team/me`, which needs no user
 * id — the session has none to give it.
 *
 * Deliberately narrower than the admin's version of this form: no notes and
 * no profession. The first is the company's private record ABOUT you and is
 * not even returned here; the second is how your employer classifies you for
 * assignment. The backend enforces both by not having the fields on the
 * request model at all, so this component is the polite explanation, not the
 * control.
 */
export function MyProfilePanel() {
  const { accessToken } = useAuth();
  const [profile, setProfile] = React.useState<TeamMember | null>(null);
  const [draft, setDraft] = React.useState<Draft>(EMPTY);
  const [phones, setPhones] = React.useState<PhoneEntry[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [saved, setSaved] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [photoFile, setPhotoFile] = React.useState<File | null>(null);
  const [uploading, setUploading] = React.useState(false);
  const photoInputRef = React.useRef<HTMLInputElement>(null);

  // Seeded once, for the reason the team record page carries the same ref:
  // the access token is replaced on a timer, which re-creates this loader,
  // and the loader's target is the form the user may be halfway through.
  const seeded = React.useRef(false);
  const beginLoad = useLatestOnly();

  const load = React.useCallback(async () => {
    if (!accessToken || seeded.current) return;
    const isCurrent = beginLoad();
    try {
      const response = await fetch("/api/team/me", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!isCurrent()) return;
      if (!response.ok) {
        setError(data.detail ?? "Failed to load your profile");
        return;
      }
      seeded.current = true;
      setProfile(data);
      setDraft(draftFrom(data));
      setPhones(data.phones);
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
    if (saving || !accessToken || !profile) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const response = await fetch("/api/team/me", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          // Blanks included: an emptied input has to reach the backend as
          // null or a field could never be cleared.
          ...Object.fromEntries(
            Object.entries(draft).map(([key, value]) => [key, value.trim() || null])
          ),
          phones: filledPhones(phones),
          expected_updated_at: profile.updated_at,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(
          response.status === 409
            ? "Somebody in your company changed this record while you had it open. Reload to see their version, then re-apply your changes."
            : (data.detail ?? "Failed to save your profile")
        );
        return;
      }
      setProfile(data);
      setDraft(draftFrom(data));
      setPhones(data.phones);
      setSaved(true);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSaving(false);
    }
  }

  async function uploadPhoto() {
    if (uploading || !accessToken || !photoFile) return;
    setUploading(true);
    setError(null);
    try {
      const body = new FormData();
      body.append("file", photoFile);
      const response = await fetch("/api/team/me/photo", {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
        body,
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to upload the photo");
        return;
      }
      setProfile(data);
      setPhotoFile(null);
      if (photoInputRef.current) photoInputRef.current.value = "";
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setUploading(false);
    }
  }

  if (!profile) {
    return (
      <section className="flex max-w-2xl flex-col gap-2 rounded-lg border border-slate-200 p-4">
        <h2 className="text-sm font-semibold">Your details</h2>
        {error ? (
          <p role="alert" aria-live="assertive" className="text-sm text-red-600">
            {error}
          </p>
        ) : (
          <p className="text-sm text-slate-600">Loading…</p>
        )}
      </section>
    );
  }

  const displayName =
    [profile.first_name, profile.last_name].filter(Boolean).join(" ").trim() ||
    profile.account_name ||
    profile.email;

  return (
    <section className="flex max-w-2xl flex-col gap-4 rounded-lg border border-slate-200 p-4">
      <div className="flex items-center gap-4">
        <MemberPhoto
          src="/api/team/me/photo"
          hasPhoto={profile.has_photo}
          name={displayName}
          version={profile.updated_at}
          size={56}
        />
        <div>
          <h2 className="text-sm font-semibold">Your details</h2>
          <p className="text-xs text-slate-500">
            How your company can reach you. Everyone who can see the team directory sees this.
          </p>
        </div>
      </div>

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {saved && !error && (
        <p role="status" className="text-sm text-green-700">
          Saved.
        </p>
      )}

      <form className="flex flex-col gap-4" onSubmit={save}>
        <div className="grid gap-4 sm:grid-cols-2">
          {FIELDS.map((field) => (
            <div key={field.key} className="flex flex-col gap-1.5">
              <Label htmlFor={`me-${field.key}`}>{field.label}</Label>
              <Input
                id={`me-${field.key}`}
                value={draft[field.key]}
                maxLength={field.max}
                disabled={saving}
                onChange={(event) =>
                  setDraft((previous) => ({ ...previous, [field.key]: event.target.value }))
                }
              />
            </div>
          ))}
        </div>

        <PhonesEditor phones={phones} onChange={setPhones} disabled={saving} idPrefix="me-phone" />

        <div>
          <Button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>

      <div className="flex flex-col gap-2 border-t border-slate-200 pt-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="me-photo">Photo (PNG or JPEG, up to 2 MB)</Label>
          <Input
            id="me-photo"
            type="file"
            ref={photoInputRef}
            accept="image/png,image/jpeg"
            disabled={uploading}
            onChange={(event) => setPhotoFile(event.target.files?.[0] ?? null)}
          />
        </div>
        <div>
          <Button
            type="button"
            variant="outline"
            disabled={uploading || !photoFile}
            onClick={() => void uploadPhoto()}
          >
            {uploading ? "Uploading…" : "Upload photo"}
          </Button>
        </div>
      </div>

      {profile.profession && (
        <p className="text-xs text-slate-500">
          Filed under <span className="font-medium">{profile.profession.name}</span> by your
          company. An admin can change that.
        </p>
      )}
    </section>
  );
}
