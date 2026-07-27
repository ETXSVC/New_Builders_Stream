/**
 * Settings every Sentry runtime here shares, and the reasoning for the
 * ones that are deliberately restrictive.
 *
 * The backend half of this landed first (`backend/app/core/observability.py`)
 * and the same rule governs both: this product holds client email
 * addresses, invoice amounts, and the IP addresses captured as legal
 * evidence of e-signature acceptance (docs/07). Error reporting must make
 * incidents debuggable without exporting any of that.
 */

/**
 * Unset means OFF. Nothing initialises, and `withSentryConfig` leaves the
 * build alone.
 *
 * Note the asymmetry with the backend, which is worth understanding before
 * an incident rather than during one: the SERVER runtime reads `SENTRY_DSN`
 * at run time, so it behaves like the backend — set it and restart. The
 * BROWSER runtime cannot; `NEXT_PUBLIC_SENTRY_DSN` is inlined into the
 * client bundle at build time, so turning client-side reporting on requires
 * a rebuild. That is inherent to shipping a DSN to a browser, not something
 * this configuration chose. (A Sentry DSN is public by design — it is a
 * write-only ingest key — so being in the bundle is not itself a leak.)
 */
export const CLIENT_DSN = process.env.NEXT_PUBLIC_SENTRY_DSN;
export const SERVER_DSN = process.env.SENTRY_DSN ?? process.env.NEXT_PUBLIC_SENTRY_DSN;

export const ENVIRONMENT = process.env.NEXT_PUBLIC_APP_ENV ?? "development";

/**
 * Traces off unless asked for. Errors are the stated need; traces on a
 * single-box deployment mostly spend quota.
 */
export const TRACES_SAMPLE_RATE = Number(process.env.NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE ?? "0");

/**
 * Query-string keys whose values never leave the browser.
 *
 * `id` is in this list and that is not an oversight. The invitation-accept
 * page reads its invitation from `?id=` (`app/(app)/accept-invitation/`),
 * and that value IS the authorisation to join a company — a crash on that
 * page would otherwise report a working invite link into a third-party
 * service. An earlier version of this list carried `token` and
 * `invitation` on the assumption the parameter was named one of those; a
 * test against the real URL shape proved otherwise.
 *
 * The cost is that a benign `?id=` elsewhere is redacted too. That is
 * cheap: the path already identifies the page, and a UUID in a query
 * string is rarely what makes an error report actionable.
 */
const SENSITIVE_QUERY_KEYS = [
  "id",
  "token",
  "code",
  "state",
  "invitation",
  "id_token",
  "access_token",
];

/**
 * Strip credentials out of any URL before it is reported.
 *
 * The invitation-accept page is the concrete case: `/accept-invitation?id=<uuid>`
 * is the whole authorisation to join a company. A crash on that page would
 * otherwise report a working invite link.
 */
export function scrubUrl(raw: string): string {
  try {
    const url = new URL(raw, "http://placeholder.invalid");
    let touched = false;
    for (const key of SENSITIVE_QUERY_KEYS) {
      if (url.searchParams.has(key)) {
        url.searchParams.set(key, "[redacted]");
        touched = true;
      }
    }
    if (!touched) return raw;
    return raw.startsWith("http") ? url.toString() : `${url.pathname}${url.search}${url.hash}`;
  } catch {
    // A value we cannot parse is a value we cannot safely inspect.
    return raw;
  }
}

/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * Applied to every event on its way out.
 *
 * Deliberately narrow. A scrubber that strips everything produces events
 * nobody can act on, and the next person turns the whole integration off —
 * so this removes credentials and leaves the rest.
 */
export function scrubEvent(event: any): any {
  if (event?.request?.url) event.request.url = scrubUrl(event.request.url);
  // Headers are set server-side; the browser SDK does not attach them, but
  // the server runtime does.
  const headers = event?.request?.headers;
  if (headers && typeof headers === "object") {
    for (const name of Object.keys(headers)) {
      if (["authorization", "cookie", "x-tenant-id"].includes(name.toLowerCase())) {
        headers[name] = "[redacted]";
      }
    }
  }
  for (const crumb of event?.breadcrumbs ?? []) {
    if (typeof crumb?.data?.url === "string") crumb.data.url = scrubUrl(crumb.data.url);
    if (typeof crumb?.message === "string" && crumb.message.includes("?")) {
      crumb.message = scrubUrl(crumb.message);
    }
  }
  return event;
}
