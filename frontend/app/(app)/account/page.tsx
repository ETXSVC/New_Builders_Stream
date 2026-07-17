"use client";

import { useAuth } from "@/contexts/AuthContext";
import { MfaPanel } from "@/components/account/MfaPanel";

export default function AccountPage() {
  const { mfaEnrollmentRequired } = useAuth();

  return (
    <main className="p-6 flex flex-col gap-6">
      <h1 className="text-xl font-semibold">Account</h1>
      {mfaEnrollmentRequired && (
        <p className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-md p-3 max-w-md">
          As an admin, you should enable two-factor authentication.
        </p>
      )}
      {/* mfaEnrollmentRequired=true implies MFA is not yet active (spec:
          the flag is only true when the admin hasn't activated MFA) — a
          safe, if slightly indirect, proxy for MfaPanel's mfaActive prop
          until a dedicated "am I MFA-active" field exists on the client. */}
      <MfaPanel mfaActive={!mfaEnrollmentRequired} />
    </main>
  );
}
