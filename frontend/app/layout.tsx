import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Builders Stream | Construction work, in one clear flow",
  description: "A connected operating system for growing construction and renovation teams.",
};

/**
 * Every page renders per request, and the CSP is the reason.
 *
 * `middleware.ts` mints a nonce per response and Next stamps it into the
 * inline bootstrap scripts of the page it renders. Prerendered HTML cannot
 * carry a per-request value: it would ship whatever nonce existed at build
 * time, the browser would compare it against the header's fresh one, and
 * every page would fail to hydrate under its own policy — silently, since
 * nothing in the app itself would have errored.
 *
 * So this is the cost of removing `'unsafe-inline'` from `script-src`,
 * accepted deliberately: no static prerendering. It is a small price here
 * — this is an authenticated product behind a login, not a content site —
 * but it is a real one, and it belongs stated where it takes effect rather
 * than in a commit message nobody will read again.
 */
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
