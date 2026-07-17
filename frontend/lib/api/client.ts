import type { paths } from "./types";

const BACKEND_API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Method = "get" | "post" | "put" | "delete";

interface RequestOptions {
  accessToken?: string;
  companyId?: string;
  body?: unknown;
  params?: Record<string, string>;
}

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
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

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (options.accessToken) headers["Authorization"] = `Bearer ${options.accessToken}`;
  if (options.companyId) headers["X-Tenant-ID"] = options.companyId;

  const response = await fetch(url, {
    method: method.toUpperCase(),
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const errorBody = await response.json();
      if (typeof errorBody.detail === "string") detail = errorBody.detail;
    } catch {
      // response body wasn't JSON — fall back to statusText, already set
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) return undefined;
  return response.json();
}
