/**
 * Browser-side error reporting. Inert unless NEXT_PUBLIC_SENTRY_DSN was set
 * at build time.
 *
 * Two choices here are specific to this product rather than defaults:
 *
 * **Session Replay is off, and should stay off.** Replay records the DOM.
 * The screens it would capture are the e-signature flow, client names and
 * addresses, invoice and payment amounts, and the compliance documents
 * attached to subcontractors. That is precisely the material docs/07 treats
 * as sensitive, and shipping it to a third party to make debugging easier
 * is not a trade this product gets to make quietly. If it is ever wanted,
 * it needs `maskAllText` and `blockAllMedia` at minimum, plus a decision
 * that is written down.
 *
 * **Events tunnel through the app's own origin** (`tunnelRoute` in
 * next.config.ts) rather than going straight to ingest.sentry.io. That
 * keeps `connect-src 'self'` in the CSP intact — see next.config.ts.
 */
import * as Sentry from "@sentry/nextjs";

import { CLIENT_DSN, ENVIRONMENT, TRACES_SAMPLE_RATE, scrubEvent } from "./sentry.shared";

if (CLIENT_DSN) {
  Sentry.init({
    dsn: CLIENT_DSN,
    environment: ENVIRONMENT,
    // Never attach IPs, cookies or user identity. The backend tags the
    // verified company_id server-side, which is the useful signal anyway.
    sendDefaultPii: false,
    tracesSampleRate: TRACES_SAMPLE_RATE,
    // No replayIntegration — see the module docstring.
    integrations: [],
    beforeSend: scrubEvent,
    beforeBreadcrumb(crumb) {
      // Console breadcrumbs echo whatever the app logged, which on this
      // frontend includes API error bodies. Drop them rather than audit
      // every console call forever.
      return crumb.category === "console" ? null : crumb;
    },
  });
}

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
