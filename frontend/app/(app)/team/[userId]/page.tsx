"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import type { components } from "@/lib/api/types";
import { useAuth } from "@/contexts/AuthContext";
import { useLatestOnly } from "@/lib/use-latest-only";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { MemberPhoto } from "@/components/team/MemberPhoto";
import { displayName } from "@/components/team/display-name";

type TeamMember = components["schemas"]["TeamMemberResponse"];
type Profession = components["schemas"]["ProfessionResponse"];
type PhoneEntry = components["schemas"]["PhoneEntry"];

/** The text fields, listed once so the form and the payload cannot drift. */
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
type Draft = Record<FieldKey, string> & { notes: string; profession_id: string };

const EMPTY_DRAFT: Draft = {
  first_name: "",
  last_name: "",
  address_line1: "",
  address_line2: "",
  city: "",
  state: "",
  postal_code: "",
  notes: "",
  profession_id: "",
};

function draftFrom(member: TeamMember): Draft {
  return {
    first_name: member.first_name ?? "",
    last_name: member.last_name ?? "",
    address_line1: member.address_line1 ?? "",
    address_line2: member.address_line2 ?? "",
    city: member.city ?? "",
    state: member.state ?? "",
    postal_code: member.postal_code ?? "",
    notes: member.notes ?? "",
    profession_id: member.profession?.id ?? "",
  };
}

/**
 * One person's record: what this company knows about them.
 *
 * Reads are `admin` + `project_manager` (an assignee picker needs to see who
 * exists); every write here is `admin` only, because a directory entry is an
 * HR record. A project manager therefore gets this page in full, without the
 * controls — rather than a 403 they cannot act on.
 *
 * The save sends every field on the form, including the blank ones, which is
 * how a field gets CLEARED: the backend distinguishes an omitted key from an
 * explicit null, and an emptied input means null here rather than "". Phones
 * are sent as the whole list because that is the API's shape — there are no
 * per-phone ids to add or remove against.
 */
export default function TeamMemberPage() {
  const { userId } = useParams<{ userId: string }>();
  const { accessToken, role } = useAuth();

  const [member, setMember] = React.useState<TeamMember | null>(null);
  const [professions, setProfessions] = React.useState<Profession[]>([]);
  const [draft, setDraft] = React.useState<Draft>(EMPTY_DRAFT);
  const [phones, setPhones] = React.useState<PhoneEntry[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [saved, setSaved] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [photoFile, setPhotoFile] = React.useState<File | null>(null);
  const [uploading, setUploading] = React.useState(false);
  const photoInputRef = React.useRef<HTMLInputElement>(null);

  const canEdit = role === "admin";

  const beginLoad = useLatestOnly();
  const load = React.useCallback(async () => {
    if (!accessToken) return;
    const isCurrent = beginLoad();
    try {
      const [memberResponse, professionsResponse] = await Promise.all([
        fetch(`/api/team/${userId}`, { headers: { Authorization: `Bearer ${accessToken}` } }),
        fetch("/api/team/professions", { headers: { Authorization: `Bearer ${accessToken}` } }),
      ]);
      const memberData = await memberResponse.json();
      if (!isCurrent()) return;
      if (!memberResponse.ok) {
        setError(memberData.detail ?? "Failed to load this team member");
        return;
      }
      setMember(memberData);
      setDraft(draftFrom(memberData));
      setPhones(memberData.phones);
      if (professionsResponse.ok) setProfessions(await professionsResponse.json());
    } catch {
      if (isCurrent()) {
        setError("Unable to reach the server. Check your connection and try again.");
      }
    }
  }, [accessToken, beginLoad, userId]);

  React.useEffect(() => {
    // Deferred to a promise callback, like every other loader here: a
    // setState reached synchronously from an effect body is a cascading
    // render, and the lint rule says so.
    void Promise.resolve().then(() => load());
  }, [load]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving || !accessToken || !member) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const response = await fetch(`/api/team/${userId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          ...Object.fromEntries(
            Object.entries(draft).map(([key, value]) => [key, value.trim() || null])
          ),
          // Only the numbers that were actually filled in. A row left blank
          // by someone who clicked "Add a phone" and changed their mind is
          // not a phone number.
          phones: phones.filter((phone) => phone.number.trim() !== ""),
          // Opt-in optimistic concurrency (app/services/concurrency.py). Null
          // for a member whose profile row does not exist yet, which is the
          // state a first edit starts from — there is nothing to conflict
          // with until this save creates it.
          expected_updated_at: member.updated_at,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(
          response.status === 409
            ? "Somebody else saved this record while you had it open. Reload to see their changes, then re-apply yours."
            : (data.detail ?? "Failed to save this team member")
        );
        return;
      }
      setMember(data);
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
      const response = await fetch(`/api/team/${userId}/photo`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
        body,
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to upload the photo");
        return;
      }
      setMember(data);
      setPhotoFile(null);
      // The input keeps the old filename otherwise, which reads as though
      // the upload had not happened.
      if (photoInputRef.current) photoInputRef.current.value = "";
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setUploading(false);
    }
  }

  if (!member) {
    return (
      <main className="flex max-w-3xl flex-col gap-4 p-6">
        <Link href="/team" className="text-sm text-slate-600 hover:text-slate-900">
          ← Team
        </Link>
        {error ? (
          <p role="alert" aria-live="assertive" className="text-sm text-red-600">
            {error}
          </p>
        ) : (
          <p className="text-sm text-slate-600">Loading…</p>
        )}
      </main>
    );
  }

  return (
    <main className="flex max-w-3xl flex-col gap-6 p-6">
      <div className="flex flex-col gap-3">
        <Link href="/team" className="text-sm text-slate-600 hover:text-slate-900">
          ← Team
        </Link>
        <div className="flex items-center gap-4">
          <MemberPhoto
            userId={member.user_id}
            hasPhoto={member.has_photo}
            name={displayName(member)}
            size={64}
          />
          <div>
            <h1 className="text-xl font-semibold">{displayName(member)}</h1>
            <p className="text-sm text-slate-500">
              {member.email} · {member.role}
            </p>
          </div>
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

      {!canEdit && (
        <p className="rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-600">
          Only an admin can change a team member&apos;s record.
        </p>
      )}

      <form className="flex flex-col gap-4" onSubmit={save}>
        <div className="grid gap-4 sm:grid-cols-2">
          {FIELDS.map((field) => (
            <div key={field.key} className="flex flex-col gap-1.5">
              <Label htmlFor={`team-${field.key}`}>{field.label}</Label>
              <Input
                id={`team-${field.key}`}
                value={draft[field.key]}
                maxLength={field.max}
                disabled={!canEdit || saving}
                onChange={(event) =>
                  setDraft((previous) => ({ ...previous, [field.key]: event.target.value }))
                }
              />
            </div>
          ))}

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="team-profession">Profession</Label>
            <Select
              id="team-profession"
              value={draft.profession_id}
              disabled={!canEdit || saving}
              onChange={(event) =>
                setDraft((previous) => ({ ...previous, profession_id: event.target.value }))
              }
            >
              <option value="">(none)</option>
              {professions.map((profession) => (
                <option key={profession.id} value={profession.id}>
                  {profession.name}
                </option>
              ))}
            </Select>
          </div>
        </div>

        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-medium">Phone numbers</legend>
          {phones.map((phone, index) => (
            <div key={index} className="flex items-end gap-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`phone-label-${index}`}>Label</Label>
                <Input
                  id={`phone-label-${index}`}
                  value={phone.label ?? ""}
                  maxLength={50}
                  placeholder="mobile"
                  disabled={!canEdit || saving}
                  className="w-40"
                  onChange={(event) =>
                    setPhones((previous) =>
                      previous.map((entry, i) =>
                        i === index ? { ...entry, label: event.target.value } : entry
                      )
                    )
                  }
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`phone-number-${index}`}>Number</Label>
                <Input
                  id={`phone-number-${index}`}
                  value={phone.number}
                  maxLength={40}
                  disabled={!canEdit || saving}
                  className="w-56"
                  onChange={(event) =>
                    setPhones((previous) =>
                      previous.map((entry, i) =>
                        i === index ? { ...entry, number: event.target.value } : entry
                      )
                    )
                  }
                />
              </div>
              {canEdit && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={saving}
                  aria-label={`Remove phone ${index + 1}`}
                  onClick={() => setPhones((previous) => previous.filter((_, i) => i !== index))}
                >
                  Remove
                </Button>
              )}
            </div>
          ))}
          {phones.length === 0 && <p className="text-sm text-slate-600">No phone numbers.</p>}
          {canEdit && (
            <div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={saving}
                onClick={() => setPhones((previous) => [...previous, { label: "", number: "" }])}
              >
                Add a phone
              </Button>
            </div>
          )}
        </fieldset>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="team-notes">Notes</Label>
          <Textarea
            id="team-notes"
            value={draft.notes}
            disabled={!canEdit || saving}
            onChange={(event) =>
              setDraft((previous) => ({ ...previous, notes: event.target.value }))
            }
          />
        </div>

        {canEdit && (
          <div>
            <Button type="submit" disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </Button>
          </div>
        )}
      </form>

      {canEdit && (
        <section className="flex flex-col gap-2 rounded-lg border border-slate-200 p-4">
          <h2 className="text-sm font-semibold">Photo</h2>
          <p className="text-xs text-slate-500">PNG or JPEG, up to 2 MB.</p>
          <input
            ref={photoInputRef}
            id="team-photo"
            type="file"
            accept="image/png,image/jpeg"
            className="text-sm"
            disabled={uploading}
            onChange={(event) => setPhotoFile(event.target.files?.[0] ?? null)}
          />
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
        </section>
      )}
    </main>
  );
}
