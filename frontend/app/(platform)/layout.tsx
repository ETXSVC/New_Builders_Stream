import "./console.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Platform console | Builders Stream",
  // The console is operator-facing and cross-tenant. Keeping it out of
  // indexes costs nothing and is one less way to discover it exists.
  robots: { index: false, follow: false },
};

/**
 * Deliberately NOT wrapped in AuthProvider or AppShell.
 *
 * The console is a different trust tier with a different token
 * (`lib/platform/session.ts`), and the product's nav would offer a signed-in
 * platform operator links to tenant routes their token cannot open. Sharing
 * the shell would also mean `useAuth()` running here, which throws without a
 * provider and would tempt someone to add one.
 */
export default function PlatformLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <div className="min-h-screen bg-slate-100 text-slate-900">{children}</div>;
}
