import type { components } from "@/lib/api/types";

type TeamMember = components["schemas"]["TeamMemberResponse"];

/**
 * The company's name for this person, falling back to the account's.
 *
 * They are deliberately separate on the backend: `users.full_name` is the
 * name the account holder set for themselves, and `first_name`/`last_name`
 * are the employer's own record of them, separately editable. Showing the
 * account name until somebody fills the record in beats showing a blank row
 * — and the email is the last resort, because every member has one.
 */
export function displayName(member: TeamMember): string {
  const filed = [member.first_name, member.last_name].filter(Boolean).join(" ").trim();
  return filed || member.account_name || member.email;
}
