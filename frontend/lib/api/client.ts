import "server-only";

import type { paths } from "./types";

// Exported for the two Route Handlers that can't use the JSON-only
// apiFetch below (document multipart upload and download streaming) and
// must build their backend URL directly — still server-side only.
export const BACKEND_API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Method = "get" | "post" | "put" | "patch" | "delete";

interface RequestOptions {
  accessToken?: string;
  companyId?: string;
  body?: unknown;
  params?: Record<string, string>;
  // Appended as a query string (?k=v&...). Entries with undefined values
  // are skipped, so callers can pass optional filters unconditionally.
  query?: Record<string, string | undefined>;
  // Forwarded to the backend as X-Forwarded-For. The BFF hop is a fresh
  // server-side fetch, so without this the backend's peer address is the
  // frontend container — the register rate limiter and the ESIGN capture
  // (legally significant) would record the wrong IP behind the production
  // proxy. Only the IP-sensitive route handlers pass it, sourced from the
  // reverse proxy's own spoof-safe X-Forwarded-For header; undefined
  // (e.g. bare `npm run dev` with no proxy) omits the header entirely.
  clientIp?: string;
}

export class ApiError extends Error {
  status: number;
  detail: string;
  /**
   * The backend's `detail` when it is a structured object rather than a
   * string — currently `PUT /estimates/{id}/lines`'s 409, which carries the
   * conflicting rates so the client can offer to adopt them without a
   * second round trip.
   *
   * Kept ALONGSIDE `detail` rather than widening it: `detail` is rendered
   * as text in a dozen components, and making it `string | object` would
   * put "[object Object]" in a banner the first time somebody forgot to
   * narrow it. `detail` stays the human sentence; this carries the data.
   */
  detailPayload?: Record<string, unknown>;

  constructor(status: number, detail: string, detailPayload?: Record<string, unknown>) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.detailPayload = detailPayload;
  }
}

/**
 * Server-side-only typed fetch wrapper. Never import this into a client
 * component — it's designed to run inside Route Handlers, which are the
 * only code in this app that talks to the FastAPI backend directly (the
 * backend-for-frontend pattern, spec Decision 5).
 */
export async function apiFetch<Path extends keyof paths, M extends Method>(
  path: Path,
  method: M,
  options: RequestOptions = {}
): Promise<unknown> {
  let url = `${BACKEND_API_URL}${String(path)}`;
  if (options.params) {
    for (const [key, value] of Object.entries(options.params)) {
      url = url.replace(`{${key}}`, encodeURIComponent(value));
    }
  }
  // A mistyped params key (e.g. companyId instead of company_id) would
  // otherwise leave the literal "{param}" in the URL and fail silently
  // against the backend instead of at the call site.
  if (/\{[^}]+\}/.test(url)) {
    throw new Error(`apiFetch: unresolved path parameter(s) in "${url}" — check the params keys`);
  }

  if (options.query) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(options.query)) {
      if (value !== undefined) search.set(key, value);
    }
    const qs = search.toString();
    if (qs) url += `?${qs}`;
  }

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (options.accessToken) headers["Authorization"] = `Bearer ${options.accessToken}`;
  if (options.companyId) headers["X-Tenant-ID"] = options.companyId;
  if (options.clientIp) headers["X-Forwarded-For"] = options.clientIp;

  const response = await fetch(url, {
    method: method.toUpperCase(),
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    let detail = response.statusText;
    let detailPayload: Record<string, unknown> | undefined;
    try {
      const errorBody = await response.json();
      if (typeof errorBody.detail === "string") {
        detail = errorBody.detail;
      } else if (errorBody.detail && typeof errorBody.detail === "object" && !Array.isArray(errorBody.detail)) {
        // A structured detail — an HTTPException raised with a dict rather
        // than a string, because the caller has to DO something with it and
        // not merely display it.
        //
        // Before this branch existed such a body fell through to
        // `response.statusText`, so a 409 carrying a list of changed
        // catalog rates reached the browser as the single word "Conflict".
        // Nothing errored: the route returned a valid JSON error with a
        // plausible message, and the recovery UI simply never appeared.
        // Caught by e2e, invisible to typecheck and to every unit test on
        // either side of the BFF.
        detailPayload = errorBody.detail as Record<string, unknown>;
        if (typeof detailPayload.message === "string") detail = detailPayload.message;
      } else if (Array.isArray(errorBody.detail)) {
        // FastAPI's default 422 shape is HTTPValidationError: an ARRAY of
        // {loc, msg, type} objects, not a string — every field-validated
        // route (register, login, ...) can return this. Joining the msgs
        // is what makes Task 9/10's LoginForm/RegisterForm show the real
        // per-field reason instead of a generic "Unprocessable Entity".
        detail = errorBody.detail.map((e: { msg?: string }) => e.msg).filter(Boolean).join("; ") || detail;
      }
    } catch {
      // response body wasn't JSON — fall back to statusText, already set
    }
    throw new ApiError(response.status, detail, detailPayload);
  }

  if (response.status === 204) return undefined;
  return response.json();
}
