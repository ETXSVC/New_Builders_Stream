"use client";

import * as React from "react";
import Link from "next/link";
import type { components } from "@/lib/api/types";
import { useCursorList } from "@/lib/use-cursor-list";
import { Button } from "@/components/ui/button";
import { MemberPhoto } from "@/components/team/MemberPhoto";
import { ProfessionsPanel } from "@/components/team/ProfessionsPanel";
import { displayName } from "@/components/team/display-name";

type TeamMember = components["schemas"]["TeamMemberResponse"];

/**
 * Who is in this company.
 *
 * The richer sibling of the members list on the account screen: same people,
 * same role check (`admin` + `project_manager`), plus everything this
 * company records about them. Somebody who has just accepted an invitation
 * appears here immediately with only their account details — the profile row
 * is created on first edit, so an empty record is a normal state rather than
 * a missing one.
 */
export default function TeamPage() {
  const {
    items: members,
    nextCursor,
    loading,
    error,
    loadMore,
    reload,
  } = useCursorList<TeamMember>({ path: "/api/team", label: "the team" });

  return (
    <main className="flex max-w-4xl flex-col gap-4 p-6">
      <div>
        <h1 className="text-xl font-semibold">Team</h1>
        <p className="text-sm text-slate-500">
          Your company&apos;s own record of its people. Kept per company — the same person working
          for another builder has a separate record there.
        </p>
      </div>

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {!loading && members.length === 0 && !error && (
        <p className="text-sm text-slate-600">Nobody here yet.</p>
      )}

      <ul className="flex flex-col divide-y divide-slate-200 rounded-lg border border-slate-200 empty:hidden">
        {members.map((member) => (
          <li key={member.user_id}>
            <Link
              href={`/team/${member.user_id}`}
              className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
            >
              <MemberPhoto
                src={`/api/team/${member.user_id}/photo`}
                hasPhoto={member.has_photo}
                name={displayName(member)}
                version={member.updated_at}
              />
              <span className="flex flex-1 flex-col">
                <span className="text-sm font-medium">{displayName(member)}</span>
                <span className="text-xs text-slate-500">{member.email}</span>
              </span>
              <span className="text-sm text-slate-600">{member.profession?.name ?? "—"}</span>
              <span className="text-sm text-slate-500">{member.phones[0]?.number ?? ""}</span>
              <span className="text-xs uppercase tracking-wide text-slate-400">{member.role}</span>
            </Link>
          </li>
        ))}
      </ul>

      {nextCursor && (
        <Button variant="outline" onClick={loadMore} disabled={loading}>
          Load more
        </Button>
      )}

      {/* Removing a profession clears it from whoever held it, so the list
          above is re-read rather than left showing a trade that is gone. */}
      <ProfessionsPanel onChanged={reload} />
    </main>
  );
}
