/**
 * Server- and edge-runtime error reporting for the Next process.
 *
 * This is the half that behaves like the backend: `SENTRY_DSN` is read at
 * RUN time, so enabling it is an environment change plus a restart, with no
 * rebuild. The browser half cannot work that way — see sentry.shared.ts.
 *
 * The server runtime is where the BFF route handlers live, so this is what
 * reports a failure in the proxy layer between the browser and FastAPI —
 * a gap that was previously invisible from both ends.
 */
import * as Sentry from "@sentry/nextjs";

import { ENVIRONMENT, SERVER_DSN, TRACES_SAMPLE_RATE, scrubEvent } from "./sentry.shared";

export async function register() {
  if (!SERVER_DSN) return;

  Sentry.init({
    dsn: SERVER_DSN,
    environment: ENVIRONMENT,
    sendDefaultPii: false,
    tracesSampleRate: TRACES_SAMPLE_RATE,
    beforeSend: scrubEvent,
  });
}

// Next calls this for errors thrown in server components and route
// handlers. Without it those are logged by the platform and reported
// nowhere.
export const onRequestError = Sentry.captureRequestError;
