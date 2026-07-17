# Frontend Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Builders Stream its first real application UI — login, registration, an authenticated app shell, and account/MFA management — built on a backend-for-frontend session pattern so the refresh token never touches browser JS.

**Architecture:** Next.js App Router route groups split `(marketing)` (existing pages, untouched) from `(app)` (new, Tailwind + shadcn-style primitives). Next.js Route Handlers under `(app)/api/auth/*` are the only code that calls the FastAPI backend's token endpoints; they hold the refresh token in an httpOnly cookie, while the access token lives in a client-side React context for the tab's lifetime.

**Tech Stack:** Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS v4, hand-written shadcn-style primitives (Radix + class-variance-authority), openapi-typescript for generated API types, Playwright for E2E.

**Spec:** `docs/superpowers/specs/2026-07-16-frontend-foundation-design.md` — read it before starting any task.

**Worktree:** `D:\Development\New const proj mgt software\.worktrees\frontend-foundation`, branch `feature/frontend-foundation`. Frontend commands run from `<worktree>/frontend` unless noted. Backend/Postgres/Redis for live verification: the worktree's own `docker compose` stack (same pattern used by every prior backend feature this project — stop the main-repo compose project first, `docker compose up -d --build` from the worktree root).

**Existing conventions to follow:**
- `NEXT_PUBLIC_API_URL` is already wired in `docker-compose.yml`'s frontend service (`http://backend:8000`) — reuse it rather than inventing a new env var name, even though only server-side Route Handlers will read it (nothing in this plan imports it into a client component, so it never reaches the browser bundle despite the `NEXT_PUBLIC_` prefix).
- Marketing pages (`about/`, `security/`, `solutions/`, `early-access/`, `page.tsx`) currently import shared chrome from `../components` (relative import) — when moved into a route group, that relative path is unchanged as long as the whole subtree moves together.
- `frontend/tests/` doesn't exist yet — Playwright specs go in `frontend/e2e/` (kept separate from any future component/unit test directory).
- **`frontend/tsconfig.json` self-normalizes on every `next dev`/`next build`**: Next 13+'s App Router rewrites `"jsx"` to `"react-jsx"` and appends `.next/types/**/*.ts` + `.next/dev/types/**/*.ts` to `"include"` (needed for its typed-routes feature) — confirmed stable/idempotent (two consecutive dev-server runs produced zero further drift) during Task 1. This is correct, expected behavior, not a regression from Task 1's original `"preserve"`/short-include-list text — do NOT revert it in any later task's verification step; it will just drift right back on the next `npm run dev`.
- **Every entry in `package.json` is exact-pinned (no caret/tilde), including devDependencies** — Task 1 fixed `next` (bumped off the originally-scaffolded `16.0.0`, a critical CVSS 10.0 RCE, GHSA-9qr9-h5gf-34mp, to `16.2.10`), and Task 2 fixed the three Tailwind packages that landed with carets by default. Every later task's `npm install -D <package>` step must add `--save-exact` (or hand-edit the resulting caret to an exact version before committing) — don't let this recur a third time.

---

### Task 1: Marketing pages into a route group + path alias

**Files:**
- Create: `frontend/app/(marketing)/` (new directory)
- Move: `frontend/app/page.tsx` → `frontend/app/(marketing)/page.tsx`
- Move: `frontend/app/about/page.tsx` → `frontend/app/(marketing)/about/page.tsx`
- Move: `frontend/app/early-access/page.tsx` → `frontend/app/(marketing)/early-access/page.tsx`
- Move: `frontend/app/security/page.tsx` → `frontend/app/(marketing)/security/page.tsx`
- Move: `frontend/app/solutions/page.tsx` → `frontend/app/(marketing)/solutions/page.tsx`
- Move: `frontend/app/components.tsx` → `frontend/app/(marketing)/components.tsx`
- Move: `frontend/app/styles.css` → `frontend/app/(marketing)/styles.css`
- Create: `frontend/app/(marketing)/layout.tsx`
- Modify: `frontend/app/layout.tsx` (root layout — strip the CSS import, keep metadata + html/body)
- Modify: `frontend/tsconfig.json` (add `@/*` path alias)

Route groups (`(name)`) don't add a URL segment — `/`, `/about`, `/security`, `/solutions`, `/early-access` all resolve identically after this move. Next.js also route-based-code-splits CSS: a stylesheet imported in a nested layout only loads for routes under that segment, which is what makes it safe to scope Tailwind (Task 2) to `(app)` alone without it leaking into the marketing pages' hand-written CSS.

- [ ] **Step 1: Move the six marketing files as a unit**

```bash
mkdir -p "frontend/app/(marketing)"
git mv frontend/app/page.tsx "frontend/app/(marketing)/page.tsx"
git mv frontend/app/about "frontend/app/(marketing)/about"
git mv frontend/app/early-access "frontend/app/(marketing)/early-access"
git mv frontend/app/security "frontend/app/(marketing)/security"
git mv frontend/app/solutions "frontend/app/(marketing)/solutions"
git mv frontend/app/components.tsx "frontend/app/(marketing)/components.tsx"
git mv frontend/app/styles.css "frontend/app/(marketing)/styles.css"
```

Do not edit the moved files' contents — their `../components` imports resolve correctly unchanged because `about/`, `security/`, `solutions/`, `early-access/` moved alongside `components.tsx`.

- [ ] **Step 2: Create the marketing group's own layout, carrying the CSS import**

`frontend/app/(marketing)/layout.tsx`:

```tsx
import "./styles.css";

export default function MarketingLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <>{children}</>;
}
```

- [ ] **Step 3: Strip the CSS import from the root layout, keep metadata**

Replace `frontend/app/layout.tsx` entirely with:

```tsx
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Builders Stream | Construction work, in one clear flow",
  description: "A connected operating system for growing construction and renovation teams.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
```

- [ ] **Step 4: Add the `@/*` path alias**

In `frontend/tsconfig.json`, add `baseUrl` and `paths` to `compilerOptions` (insert after `"target": "ES2022",`):

```json
    "baseUrl": ".",
    "paths": {
      "@/*": ["./*"]
    },
```

- [ ] **Step 5: Verify the marketing site is unchanged**

```bash
cd frontend && npm run dev
```

In another terminal: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/` and the same for `/about`, `/security`, `/solutions`, `/early-access` — expect `200` for each. Stop the dev server (Ctrl+C) once confirmed.

- [ ] **Step 6: Commit**

```bash
git add -A frontend/app frontend/tsconfig.json
git commit -m "refactor: move marketing pages into a (marketing) route group"
```

---

### Task 2: Tailwind CSS v4, scoped to the (app) group

**Files:**
- Modify: `frontend/package.json` (devDependencies)
- Create: `frontend/postcss.config.mjs`
- Create: `frontend/app/(app)/globals.css`
- Create: `frontend/app/(app)/layout.tsx` (minimal — just imports the CSS for now; the real app shell lands in Task 13)

- [ ] **Step 1: Install Tailwind v4**

```bash
cd frontend && npm install -D tailwindcss @tailwindcss/postcss postcss
```

- [ ] **Step 2: PostCSS config**

`frontend/postcss.config.mjs`:

```js
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
```

- [ ] **Step 3: App-group globals with the Tailwind import**

```bash
mkdir -p "frontend/app/(app)"
```

`frontend/app/(app)/globals.css`:

```css
@import "tailwindcss";
```

- [ ] **Step 4: Minimal `(app)` layout so the CSS actually loads for this segment**

`frontend/app/(app)/layout.tsx`:

```tsx
import "./globals.css";

export default function AppLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <>{children}</>;
}
```

- [ ] **Step 5: Verify Tailwind utilities work and marketing pages are untouched**

Temporarily create `frontend/app/(app)/tw-check/page.tsx`:

```tsx
export default function TwCheck() {
  return <div className="bg-blue-600 text-white p-8 rounded-lg">Tailwind works</div>;
}
```

```bash
npm run dev
```

Visit `http://localhost:3000/tw-check` — expect a blue rounded box with white text (view via a screenshot or `curl -s http://localhost:3000/tw-check | grep "bg-blue-600"` to confirm the class made it into the rendered HTML — Tailwind's utility classes pass through untouched; what matters is that the compiled stylesheet link is present, checked via `curl -s http://localhost:3000/tw-check | grep -o "_next/static/css/[^\"]*\.css"` returning a path). Then re-check `http://localhost:3000/about` still renders with the marketing site's original look (no blue Tailwind reset bleeding into it — Tailwind's preflight reset only applies where its stylesheet loads, i.e. only under `(app)`).

Delete the temporary check page:

```bash
rm -rf "frontend/app/(app)/tw-check"
```

- [ ] **Step 6: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/postcss.config.mjs "frontend/app/(app)/globals.css" "frontend/app/(app)/layout.tsx"
git commit -m "feat: add Tailwind CSS v4, scoped to the (app) route group"
```

---

### Task 3: Hand-written UI primitives (Button, Input, Label, Card)

**Files:**
- Modify: `frontend/package.json`
- Create: `frontend/lib/utils.ts`
- Create: `frontend/components/ui/button.tsx`
- Create: `frontend/components/ui/input.tsx`
- Create: `frontend/components/ui/label.tsx`
- Create: `frontend/components/ui/card.tsx`

Hand-written in the shadcn/ui style (Radix primitives + class-variance-authority + Tailwind) rather than run through shadcn's interactive CLI, which prompts for choices a non-interactive plan execution can't answer deterministically. This produces the exact same component shape shadcn/ui would generate.

- [ ] **Step 1: Install the primitive-layer dependencies**

```bash
cd frontend && npm install class-variance-authority clsx tailwind-merge @radix-ui/react-label @radix-ui/react-slot lucide-react
```

- [ ] **Step 2: The `cn()` class-merge helper every primitive uses**

`frontend/lib/utils.ts`:

```ts
import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
```

- [ ] **Step 3: Button**

`frontend/components/ui/button.tsx`:

```tsx
import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2",
  {
    variants: {
      variant: {
        default: "bg-slate-900 text-white hover:bg-slate-800",
        outline: "border border-slate-300 bg-white hover:bg-slate-50",
        ghost: "hover:bg-slate-100",
      },
      size: {
        default: "h-10 px-4 py-2",
        sm: "h-9 rounded-md px-3",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
```

- [ ] **Step 4: Input**

`frontend/components/ui/input.tsx`:

```tsx
import * as React from "react";
import { cn } from "@/lib/utils";

export type InputProps = React.InputHTMLAttributes<HTMLInputElement>;

const Input = React.forwardRef<HTMLInputElement, InputProps>(({ className, type, ...props }, ref) => {
  return (
    <input
      type={type}
      className={cn(
        "flex h-10 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm placeholder:text-slate-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      ref={ref}
      {...props}
    />
  );
});
Input.displayName = "Input";

export { Input };
```

- [ ] **Step 5: Label**

`frontend/components/ui/label.tsx`:

```tsx
import * as React from "react";
import * as LabelPrimitive from "@radix-ui/react-label";
import { cn } from "@/lib/utils";

const Label = React.forwardRef<
  React.ElementRef<typeof LabelPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof LabelPrimitive.Root>
>(({ className, ...props }, ref) => (
  <LabelPrimitive.Root
    ref={ref}
    className={cn("text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70", className)}
    {...props}
  />
));
Label.displayName = LabelPrimitive.Root.displayName;

export { Label };
```

- [ ] **Step 6: Card**

`frontend/components/ui/card.tsx`:

```tsx
import * as React from "react";
import { cn } from "@/lib/utils";

const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("rounded-lg border border-slate-200 bg-white shadow-sm", className)} {...props} />
  )
);
Card.displayName = "Card";

const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => <div ref={ref} className={cn("flex flex-col gap-1.5 p-6", className)} {...props} />
);
CardHeader.displayName = "CardHeader";

const CardTitle = React.forwardRef<HTMLHeadingElement, React.HTMLAttributes<HTMLHeadingElement>>(
  ({ className, ...props }, ref) => <h3 ref={ref} className={cn("text-lg font-semibold leading-none", className)} {...props} />
);
CardTitle.displayName = "CardTitle";

const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => <div ref={ref} className={cn("p-6 pt-0", className)} {...props} />
);
CardContent.displayName = "CardContent";

export { Card, CardHeader, CardTitle, CardContent };
```

- [ ] **Step 7: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/lib/utils.ts frontend/components/ui
git commit -m "feat: hand-written UI primitives (Button, Input, Label, Card)"
```

---

### Task 4: Generated API types

**Files:**
- Modify: `frontend/package.json` (devDependencies + script)
- Create: `frontend/lib/api/types.ts` (generated, then committed)

- [ ] **Step 1: Install openapi-typescript**

```bash
cd frontend && npm install -D openapi-typescript
```

- [ ] **Step 2: Add the generation script**

In `frontend/package.json`, add to `"scripts"`:

```json
    "generate:api-types": "openapi-typescript http://localhost:8000/openapi.json -o lib/api/types.ts"
```

- [ ] **Step 3: Start the backend and generate types**

From the repo root (not the worktree — use whichever backend is already running per your environment's convention; if none is running, bring up just postgres+redis+backend from this worktree's compose file):

```bash
docker compose up -d postgres redis backend
```

Wait for it to be healthy (`docker exec <backend-container> curl -sf http://localhost:8000/health` or poll `curl http://localhost:8000/health` from the host once port 8000 is mapped), then:

```bash
cd frontend && mkdir -p lib/api && npm run generate:api-types
```

Expected: `lib/api/types.ts` is created, several hundred lines, containing a `paths` interface with entries like `"/auth/login"`, `"/leads"`, `"/projects"`, etc.

- [ ] **Step 4: Tear down**

```bash
docker compose down
```

- [ ] **Step 5: Verify the generated file compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 6: Commit**

The generated file IS committed (not gitignored) — this matches docs/03's "types generated from the backend schema, never hand-written" rule: generation is a manual/CI-triggered regeneration step, not something contributors run individually before every build, so `next build` must work from the committed file without a live backend.

```bash
git add frontend/package.json frontend/package-lock.json frontend/lib/api/types.ts
git commit -m "feat: generate API types from the backend's OpenAPI schema"
```

---

### Task 5: Typed API client wrapper

**Files:**
- Create: `frontend/lib/api/client.ts`
- Create: `frontend/.env.local.example`

The wrapper is generic over the generated `paths` type so every call site gets full request/response typing, and centralizes the two headers docs/03 Section 5.3 requires.

- [ ] **Step 1: The client**

`frontend/lib/api/client.ts`:

```ts
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
```

- [ ] **Step 2: Document the env var for local (non-Docker) dev**

`frontend/.env.local.example`:

```bash
# Only needed if running `npm run dev` OUTSIDE Docker Compose (which
# already sets this to http://backend:8000 on its internal network).
# Copy to .env.local and adjust if your local backend runs elsewhere.
NEXT_PUBLIC_API_URL=http://localhost:8000
```

- [ ] **Step 3: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Commit**

```bash
git add frontend/lib/api/client.ts frontend/.env.local.example
git commit -m "feat: typed API client wrapper for server-side backend calls"
```

---

### Task 6: AuthContext (in-memory access token + refresh scheduling)

**Files:**
- Create: `frontend/contexts/AuthContext.tsx`

Holds the access token in memory only (never localStorage/cookies — an in-memory value is lost on tab close/refresh by design, which is fine: the httpOnly refresh cookie from Task 7 is what survives a reload, and the app re-derives a fresh access token from it on mount). Schedules a proactive refresh before the 15-minute expiry.

- [ ] **Step 1: The context**

`frontend/contexts/AuthContext.tsx`:

```tsx
"use client";

import * as React from "react";

interface AuthState {
  accessToken: string | null;
  mfaEnrollmentRequired: boolean;
}

interface AuthContextValue extends AuthState {
  setSession: (accessToken: string, mfaEnrollmentRequired: boolean) => void;
  clearSession: () => void;
}

const AuthContext = React.createContext<AuthContextValue | null>(null);

// Refresh 60s before the access token's known 15-minute lifetime — see
// docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md.
const ACCESS_TOKEN_LIFETIME_MS = 15 * 60 * 1000;
const REFRESH_MARGIN_MS = 60 * 1000;

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = React.useState<AuthState>({
    accessToken: null,
    mfaEnrollmentRequired: false,
  });
  const refreshTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearSession = React.useCallback(() => {
    if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    setState({ accessToken: null, mfaEnrollmentRequired: false });
  }, []);

  const scheduleRefresh = React.useCallback(() => {
    if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    refreshTimerRef.current = setTimeout(async () => {
      const response = await fetch("/api/auth/refresh", { method: "POST" });
      if (!response.ok) {
        clearSession();
        return;
      }
      const data = await response.json();
      setState({ accessToken: data.access_token, mfaEnrollmentRequired: data.mfa_enrollment_required });
      scheduleRefresh();
    }, ACCESS_TOKEN_LIFETIME_MS - REFRESH_MARGIN_MS);
  }, [clearSession]);

  const setSession = React.useCallback(
    (accessToken: string, mfaEnrollmentRequired: boolean) => {
      setState({ accessToken, mfaEnrollmentRequired });
      scheduleRefresh();
    },
    [scheduleRefresh]
  );

  React.useEffect(() => {
    return () => {
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    };
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, setSession, clearSession }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
```

- [ ] **Step 2: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/contexts/AuthContext.tsx
git commit -m "feat: AuthContext holding the access token in memory with proactive refresh"
```

---

### Task 7: Auth Route Handlers (login, refresh, logout)

**Files:**
- Create: `frontend/app/(app)/api/auth/login/route.ts`
- Create: `frontend/app/(app)/api/auth/refresh/route.ts`
- Create: `frontend/app/(app)/api/auth/logout/route.ts`

These are the ONLY code in the app that calls the FastAPI backend's `/auth/login`, `/auth/refresh`, `/auth/logout`. Each sets or clears the `refresh_token` httpOnly cookie and returns only the access token (+ `mfa_enrollment_required`, + `default_company_id`) to the browser — the refresh secret itself never appears in a Route Handler's JSON response body.

- [ ] **Step 1: Login**

`frontend/app/(app)/api/auth/login/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

const REFRESH_COOKIE = "refresh_token";
const REFRESH_COOKIE_MAX_AGE_SECONDS = 14 * 24 * 60 * 60; // 14 days, matches the backend's refresh lifetime

export async function POST(request: NextRequest) {
  const body = await request.json();

  try {
    const data = (await apiFetch("/auth/login", "post", { body })) as {
      access_token: string;
      refresh_token: string;
      default_company_id: string;
      mfa_enrollment_required: boolean;
    };

    const response = NextResponse.json({
      access_token: data.access_token,
      default_company_id: data.default_company_id,
      mfa_enrollment_required: data.mfa_enrollment_required,
    });
    response.cookies.set(REFRESH_COOKIE, data.refresh_token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: REFRESH_COOKIE_MAX_AGE_SECONDS,
    });
    return response;
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Login failed" }, { status: 502 });
  }
}
```

- [ ] **Step 2: Refresh**

`frontend/app/(app)/api/auth/refresh/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

const REFRESH_COOKIE = "refresh_token";
const REFRESH_COOKIE_MAX_AGE_SECONDS = 14 * 24 * 60 * 60;

export async function POST(request: NextRequest) {
  const refreshToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (!refreshToken) {
    return NextResponse.json({ detail: "No session" }, { status: 401 });
  }

  try {
    const data = (await apiFetch("/auth/refresh", "post", {
      body: { refresh_token: refreshToken },
    })) as {
      access_token: string;
      refresh_token: string;
      default_company_id: string;
      mfa_enrollment_required: boolean;
    };

    const response = NextResponse.json({
      access_token: data.access_token,
      default_company_id: data.default_company_id,
      mfa_enrollment_required: data.mfa_enrollment_required,
    });
    // Rotation (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md
    // Decision 4): every refresh replaces the cookie with the new token.
    response.cookies.set(REFRESH_COOKIE, data.refresh_token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: REFRESH_COOKIE_MAX_AGE_SECONDS,
    });
    return response;
  } catch (err) {
    const response = NextResponse.json(
      { detail: err instanceof ApiError ? err.detail : "Refresh failed" },
      { status: err instanceof ApiError ? err.status : 502 }
    );
    // A dead refresh token (expired/revoked/reused) can never succeed
    // again — clear the cookie so the client doesn't keep retrying it.
    response.cookies.delete(REFRESH_COOKIE);
    return response;
  }
}
```

- [ ] **Step 3: Logout**

`frontend/app/(app)/api/auth/logout/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";

const REFRESH_COOKIE = "refresh_token";

export async function POST(request: NextRequest) {
  const refreshToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (refreshToken) {
    // Backend logout is deliberately idempotent/always-204 (spec Decision
    // 5 of the token-lifecycle design) — no need to inspect the result.
    await apiFetch("/auth/logout", "post", { body: { refresh_token: refreshToken } }).catch(() => {});
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.delete(REFRESH_COOKIE);
  return response;
}
```

- [ ] **Step 4: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 5: Commit**

```bash
git add "frontend/app/(app)/api"
git commit -m "feat: auth Route Handlers holding the refresh token in an httpOnly cookie"
```

---

### Task 8: Middleware — session cookie presence check

**Files:**
- Create: `frontend/middleware.ts`

Presence-only check (spec Decision 5): middleware doesn't decode or trust the JWT — the backend is the source of truth for token validity on the next real API call. This just keeps an unauthenticated visitor from ever rendering `/dashboard` or `/account`.

- [ ] **Step 1: The middleware**

`frontend/middleware.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";

const REFRESH_COOKIE = "refresh_token";
const PROTECTED_PREFIXES = ["/dashboard", "/account"];

export function middleware(request: NextRequest) {
  const isProtected = PROTECTED_PREFIXES.some((prefix) => request.nextUrl.pathname.startsWith(prefix));
  if (!isProtected) return NextResponse.next();

  const hasSession = request.cookies.has(REFRESH_COOKIE);
  if (!hasSession) {
    const loginUrl = new URL("/login", request.url);
    return NextResponse.redirect(loginUrl);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*", "/account/:path*"],
};
```

- [ ] **Step 2: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/middleware.ts
git commit -m "feat: middleware redirecting unauthenticated visitors away from protected routes"
```

---

### Task 9: Login page with the two-step TOTP flow

**Files:**
- Create: `frontend/components/auth/LoginForm.tsx`
- Create: `frontend/app/(app)/login/page.tsx`

- [ ] **Step 1: The form component**

`frontend/components/auth/LoginForm.tsx`:

```tsx
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function LoginForm() {
  const router = useRouter();
  const { setSession } = useAuth();
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [totpCode, setTotpCode] = React.useState("");
  // Set once the backend's first attempt (no code) comes back with
  // "TOTP code required" — reveals the second input, per the backend's
  // own two-step design (password proven first, spec Decision 6 of the
  // MFA design).
  const [needsTotp, setNeedsTotp] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          needsTotp ? { email, password, totp_code: totpCode } : { email, password }
        ),
      });
      const data = await response.json();
      if (!response.ok) {
        if (data.detail === "TOTP code required") {
          setNeedsTotp(true);
          return;
        }
        setError(data.detail ?? "Login failed");
        return;
      }
      setSession(data.access_token, data.mfa_enrollment_required);
      router.push(data.mfa_enrollment_required ? "/account" : "/dashboard");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-sm">
      {!needsTotp && (
        <>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">Password</Label>
            <Input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
        </>
      )}
      {needsTotp && (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="totp">Authenticator code</Label>
          <Input
            id="totp"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={totpCode}
            onChange={(e) => setTotpCode(e.target.value)}
            required
            autoFocus
          />
        </div>
      )}
      {error && <p className="text-sm text-red-600">{error}</p>}
      <Button type="submit" disabled={submitting}>
        {needsTotp ? "Verify" : "Log in"}
      </Button>
    </form>
  );
}
```

- [ ] **Step 2: The page**

`frontend/app/(app)/login/page.tsx`:

```tsx
import { LoginForm } from "@/components/auth/LoginForm";

export default function LoginPage() {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex flex-col gap-6 items-center">
        <h1 className="text-xl font-semibold">Log in to Builders Stream</h1>
        <LoginForm />
      </div>
    </main>
  );
}
```

- [ ] **Step 3: Wire AuthProvider into the (app) layout**

Modify `frontend/app/(app)/layout.tsx` to wrap children in the provider:

```tsx
import "./globals.css";
import { AuthProvider } from "@/contexts/AuthContext";

export default function AppLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <AuthProvider>{children}</AuthProvider>;
}
```

- [ ] **Step 4: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 5: Commit**

```bash
git add frontend/components/auth frontend/app/\(app\)/login "frontend/app/(app)/layout.tsx"
git commit -m "feat: login page with two-step TOTP challenge"
```

---

### Task 10: Register page

**Files:**
- Create: `frontend/components/auth/RegisterForm.tsx`
- Create: `frontend/app/(app)/register/page.tsx`

`POST /auth/register` doesn't return tokens (it only creates the company + admin user — confirmed against `backend/app/schemas/auth.py`'s `RegisterResponse`), so registration is followed by an explicit login call using the same credentials, reusing the login Route Handler.

- [ ] **Step 1: The form**

`frontend/components/auth/RegisterForm.tsx`:

```tsx
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function RegisterForm() {
  const router = useRouter();
  const { setSession } = useAuth();
  const [companyName, setCompanyName] = React.useState("");
  const [fullName, setFullName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const registerResponse = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          company_name: companyName,
          admin_full_name: fullName,
          admin_email: email,
          admin_password: password,
        }),
      });
      const registerData = await registerResponse.json();
      if (!registerResponse.ok) {
        setError(registerData.detail ?? "Registration failed");
        return;
      }

      const loginResponse = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const loginData = await loginResponse.json();
      if (!loginResponse.ok) {
        setError("Account created — please log in.");
        router.push("/login");
        return;
      }
      setSession(loginData.access_token, loginData.mfa_enrollment_required);
      router.push(loginData.mfa_enrollment_required ? "/account" : "/dashboard");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-sm">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="companyName">Company name</Label>
        <Input id="companyName" value={companyName} onChange={(e) => setCompanyName(e.target.value)} required minLength={2} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="fullName">Your name</Label>
        <Input id="fullName" value={fullName} onChange={(e) => setFullName(e.target.value)} required minLength={2} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="email">Email</Label>
        <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="password">Password</Label>
        <Input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} />
      </div>
      {error && <p className="text-sm text-red-600">{error}</p>}
      <Button type="submit" disabled={submitting}>
        Create account
      </Button>
    </form>
  );
}
```

- [ ] **Step 2: The page**

`frontend/app/(app)/register/page.tsx`:

```tsx
import { RegisterForm } from "@/components/auth/RegisterForm";

export default function RegisterPage() {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex flex-col gap-6 items-center">
        <h1 className="text-xl font-semibold">Create your Builders Stream account</h1>
        <RegisterForm />
      </div>
    </main>
  );
}
```

- [ ] **Step 3: A thin register Route Handler**

`frontend/app/(app)/api/auth/register/route.ts` — proxies straight through, no cookie involved (registration issues no tokens):

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

export async function POST(request: NextRequest) {
  const body = await request.json();
  try {
    const data = await apiFetch("/auth/register", "post", { body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Registration failed" }, { status: 502 });
  }
}
```

- [ ] **Step 4: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 5: Commit**

```bash
git add frontend/components/auth/RegisterForm.tsx "frontend/app/(app)/register" "frontend/app/(app)/api/auth/register"
git commit -m "feat: register page, auto-login on success"
```

---

### Task 11: App shell (Nav + company display) and dashboard placeholder

**Files:**
- Create: `frontend/components/app-shell/Nav.tsx`
- Create: `frontend/app/(app)/dashboard/page.tsx`
- Modify: `frontend/app/(app)/layout.tsx` (render Nav around children, but only for authenticated routes — see Step 3)

- [ ] **Step 1: A Route Handler exposing the current company's name**

The browser needs the active company's display name but must never call the FastAPI backend directly — another thin Route Handler, this one forwarding the caller's access token.

`frontend/app/(app)/api/companies/current/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

export async function GET(request: NextRequest) {
  const authHeader = request.headers.get("Authorization");
  const accessToken = authHeader?.startsWith("Bearer ") ? authHeader.slice(7) : undefined;
  const companyId = request.nextUrl.searchParams.get("company_id");
  if (!accessToken || !companyId) {
    return NextResponse.json({ detail: "Missing access token or company_id" }, { status: 400 });
  }
  try {
    const data = await apiFetch("/companies/{company_id}", "get", {
      accessToken,
      params: { company_id: companyId },
    });
    return NextResponse.json(data);
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Failed to load company" }, { status: 502 });
  }
}
```

- [ ] **Step 2: Nav component — company name (read-only, per spec Decision 5) + logout**

`frontend/components/app-shell/Nav.tsx`:

```tsx
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";

export function Nav({ companyId }: { companyId: string }) {
  const router = useRouter();
  const { accessToken, clearSession } = useAuth();
  const [companyName, setCompanyName] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!accessToken) return;
    fetch(`/api/companies/current?company_id=${companyId}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
      .then((r) => r.json())
      .then((data) => setCompanyName(data.name ?? null))
      .catch(() => setCompanyName(null));
  }, [accessToken, companyId]);

  async function handleLogout() {
    await fetch("/api/auth/logout", { method: "POST" });
    clearSession();
    router.push("/login");
  }

  return (
    <header className="border-b border-slate-200 px-6 py-4 flex items-center justify-between">
      <span className="font-semibold">{companyName ?? "Builders Stream"}</span>
      <div className="flex items-center gap-4">
        <a href="/account" className="text-sm text-slate-600 hover:text-slate-900">
          Account
        </a>
        <Button variant="outline" size="sm" onClick={handleLogout}>
          Log out
        </Button>
      </div>
    </header>
  );
}
```

- [ ] **Step 3: Dashboard placeholder page**

`frontend/app/(app)/dashboard/page.tsx`:

```tsx
"use client";

import { useAuth } from "@/contexts/AuthContext";
import { Nav } from "@/components/app-shell/Nav";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";

export default function DashboardPage() {
  const { accessToken } = useAuth();

  // Foundation ships no real dashboard content — every later sub-project
  // (CRM+PM, Estimation, Compliance+Billing, Invoicing, Integrations)
  // adds its own screens here. This page exists solely to prove the
  // authenticated shell renders and the session survives navigation.
  return (
    <div>
      {/* company_id will come from a real session/company-context once a
          later sub-project needs to switch companies; for Foundation the
          JWT's default_company_id (decoded client-side from the access
          token's payload — not verified client-side, display only) is
          the only company a fresh registration ever has. */}
      <Nav companyId={decodeCompanyId(accessToken)} />
      <main className="p-6">
        <Card className="max-w-md">
          <CardHeader>
            <CardTitle>Welcome</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-slate-600">
              This is a placeholder. Real project, lead, and estimate screens land in later sub-projects.
            </p>
          </CardContent>
        </Card>
      </main>
    </div>
  );
}

function decodeCompanyId(accessToken: string | null): string {
  if (!accessToken) return "";
  try {
    const payload = JSON.parse(atob(accessToken.split(".")[1]));
    return payload.default_company_id ?? "";
  } catch {
    return "";
  }
}
```

- [ ] **Step 4: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 5: Commit**

```bash
git add frontend/components/app-shell "frontend/app/(app)/dashboard" "frontend/app/(app)/api/companies"
git commit -m "feat: authenticated app shell (Nav + company display) and dashboard placeholder"
```

---

### Task 12: Account page — profile + MFA enroll/activate/disable

**Files:**
- Create: `frontend/app/(app)/api/mfa/enroll/route.ts`
- Create: `frontend/app/(app)/api/mfa/activate/route.ts`
- Create: `frontend/app/(app)/api/mfa/disable/route.ts`
- Create: `frontend/components/account/MfaPanel.tsx`
- Create: `frontend/app/(app)/account/page.tsx`

Every MFA route is authenticated (`Authorization: Bearer`), never `block_if_read_only`/`require_module`-gated on the backend (per the MFA spec), so these Route Handlers only need to forward the bearer token, not a tenant header.

- [ ] **Step 1: Enroll Route Handler**

`frontend/app/(app)/api/mfa/enroll/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

export async function POST(request: NextRequest) {
  const authHeader = request.headers.get("Authorization");
  if (!authHeader) return NextResponse.json({ detail: "Missing access token" }, { status: 401 });
  try {
    const data = await apiFetch("/auth/mfa/enroll", "post", {
      accessToken: authHeader.replace("Bearer ", ""),
    });
    const response = NextResponse.json(data);
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch (err) {
    if (err instanceof ApiError) return NextResponse.json({ detail: err.detail }, { status: err.status });
    return NextResponse.json({ detail: "Enrollment failed" }, { status: 502 });
  }
}
```

- [ ] **Step 2: Activate Route Handler**

`frontend/app/(app)/api/mfa/activate/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

export async function POST(request: NextRequest) {
  const authHeader = request.headers.get("Authorization");
  if (!authHeader) return NextResponse.json({ detail: "Missing access token" }, { status: 401 });
  const body = await request.json();
  try {
    await apiFetch("/auth/mfa/activate", "post", { accessToken: authHeader.replace("Bearer ", ""), body });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    if (err instanceof ApiError) return NextResponse.json({ detail: err.detail }, { status: err.status });
    return NextResponse.json({ detail: "Activation failed" }, { status: 502 });
  }
}
```

- [ ] **Step 3: Disable Route Handler**

`frontend/app/(app)/api/mfa/disable/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

export async function POST(request: NextRequest) {
  const authHeader = request.headers.get("Authorization");
  if (!authHeader) return NextResponse.json({ detail: "Missing access token" }, { status: 401 });
  const body = await request.json();
  try {
    await apiFetch("/auth/mfa/disable", "post", { accessToken: authHeader.replace("Bearer ", ""), body });
    // Disabling MFA revokes every refresh-token session the user holds
    // (spec Decision, docs/superpowers/specs/2026-07-16-mfa-totp-design.md)
    // — including this browser's own cookie. The client must treat this
    // as a forced logout, not just an in-place state update.
    const response = new NextResponse(null, { status: 204 });
    response.cookies.delete("refresh_token");
    return response;
  } catch (err) {
    if (err instanceof ApiError) return NextResponse.json({ detail: err.detail }, { status: err.status });
    return NextResponse.json({ detail: "Disable failed" }, { status: 502 });
  }
}
```

- [ ] **Step 4: The MFA panel component**

`frontend/components/account/MfaPanel.tsx`:

```tsx
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";

type Step = "idle" | "enrolling" | "activating";

export function MfaPanel({ mfaActive }: { mfaActive: boolean }) {
  const router = useRouter();
  const { accessToken, clearSession } = useAuth();
  const [step, setStep] = React.useState<Step>("idle");
  const [secret, setSecret] = React.useState("");
  const [totpCode, setTotpCode] = React.useState("");
  const [currentPassword, setCurrentPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  async function startEnroll() {
    setError(null);
    const response = await fetch("/api/mfa/enroll", {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    const data = await response.json();
    if (!response.ok) {
      setError(data.detail ?? "Enrollment failed");
      return;
    }
    setSecret(data.secret);
    setStep("activating");
  }

  async function confirmActivate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const response = await fetch("/api/mfa/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({ totp_code: totpCode }),
    });
    if (!response.ok) {
      const data = await response.json();
      setError(data.detail ?? "Activation failed");
      return;
    }
    setStep("idle");
    router.refresh();
  }

  async function disableMfa(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const response = await fetch("/api/mfa/disable", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({ current_password: currentPassword, totp_code: totpCode }),
    });
    if (!response.ok) {
      const data = await response.json();
      setError(data.detail ?? "Disable failed");
      return;
    }
    // Disabling MFA revoked this session's refresh token server-side
    // (see the Route Handler's comment) — treat it as a logout.
    clearSession();
    router.push("/login");
  }

  return (
    <Card className="max-w-md">
      <CardHeader>
        <CardTitle>Two-factor authentication</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && <p className="text-sm text-red-600">{error}</p>}

        {step === "idle" && !mfaActive && (
          <Button onClick={startEnroll}>Enable two-factor authentication</Button>
        )}

        {step === "activating" && (
          <form onSubmit={confirmActivate} className="flex flex-col gap-3">
            {/* No QR rendering in Foundation (would need an extra client
                dependency) — the base32 secret is the universal manual-entry
                path every authenticator app supports; a scannable QR code
                (built from the same otpauth_uri the backend also returns)
                is a natural, low-effort follow-up once this ships. */}
            <p className="text-sm text-slate-600">
              Enter this code manually in your authenticator app (Google Authenticator, 1Password, etc. all support
              &quot;Enter a setup key&quot;), then enter the 6-digit code it generates:
            </p>
            <code className="text-sm tracking-wider break-all bg-slate-50 p-2 rounded font-mono">{secret}</code>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="activate-code">Code</Label>
              <Input id="activate-code" inputMode="numeric" value={totpCode} onChange={(e) => setTotpCode(e.target.value)} required autoFocus />
            </div>
            <Button type="submit">Confirm</Button>
          </form>
        )}

        {step === "idle" && mfaActive && (
          <form onSubmit={disableMfa} className="flex flex-col gap-3">
            <p className="text-sm text-slate-600">Two-factor authentication is on. Disabling it will log you out everywhere.</p>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="disable-password">Current password</Label>
              <Input id="disable-password" type="password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} required />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="disable-code">Authenticator code</Label>
              <Input id="disable-code" inputMode="numeric" value={totpCode} onChange={(e) => setTotpCode(e.target.value)} required />
            </div>
            <Button type="submit" variant="outline">
              Disable two-factor authentication
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 5: Account page**

`frontend/app/(app)/account/page.tsx`:

```tsx
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
```

- [ ] **Step 6: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 7: Commit**

```bash
git add "frontend/app/(app)/api/mfa" frontend/components/account "frontend/app/(app)/account"
git commit -m "feat: account page with MFA enroll/activate/disable"
```

---

### Task 13: Playwright E2E — the real proof of done

**Files:**
- Modify: `frontend/package.json`
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/foundation.spec.ts`

- [ ] **Step 1: Install Playwright**

```bash
cd frontend && npm install -D @playwright/test && npx playwright install --with-deps chromium
```

- [ ] **Step 2: Config**

`frontend/playwright.config.ts`:

```ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
  },
});
```

- [ ] **Step 3: The spec**

`frontend/e2e/foundation.spec.ts`:

```ts
import { test, expect } from "@playwright/test";

test("register, land on dashboard, log out, log back in", async ({ page }) => {
  const uniqueSuffix = Date.now().toString();
  const email = `e2e-${uniqueSuffix}@foundation.test`;
  const password = "correct-horse-battery-9";

  await page.goto("/register");
  await page.getByLabel("Company name").fill(`E2E Foundation Co ${uniqueSuffix}`);
  await page.getByLabel("Your name").fill("E2E Tester");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Create account" }).click();

  await expect(page).toHaveURL(/\/dashboard/);
  await expect(page.getByText("Welcome")).toBeVisible();

  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL(/\/login/);

  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Log in" }).click();

  await expect(page).toHaveURL(/\/dashboard/);
  await expect(page.getByText("Welcome")).toBeVisible();
});
```

- [ ] **Step 4: Add the test script**

In `frontend/package.json`, add to `"scripts"`:

```json
    "test:e2e": "playwright test"
```

- [ ] **Step 5: Run it live**

From the repo root: stop the main-repo compose project first (`docker compose down`, no `-v`, from `D:\Development\New const proj mgt software` — note it needs restarting afterward), then bring up this worktree's stack:

```bash
docker compose up -d --build
```

Wait for backend health (`curl http://localhost:8000/health`), then from `frontend/`:

```bash
npm run test:e2e
```

Expected: `1 passed`. If migrations haven't run against a fresh volume, apply them host-side first (`cd ../backend && python -m alembic upgrade head`, `MIGRATIONS_DATABASE_URL` sourced from the repo-root `.env`, same as every prior feature's live-verification step).

Bring the stack down after (`docker compose down`, no `-v`), and restart the main-repo compose project (`docker compose up -d` from the repo root).

- [ ] **Step 6: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/playwright.config.ts frontend/e2e
git commit -m "test: Playwright E2E covering register, dashboard, logout, re-login"
```

---

### Task 14: Frontend CI

**Files:**
- Create: `.github/workflows/frontend-ci.yml`

Mirrors `backend-ci.yml`'s shape (per docs/10 Section 8's "lint/type-check for both backend and frontend" requirement, only half of which currently exists). `next build` type-checks as a side effect of its TypeScript compile — no separate `tsc --noEmit` step is needed in CI once this exists, though the plan's earlier tasks used it locally for fast feedback. The Playwright E2E spec is NOT wired into this workflow (spec Decision 7 — matches the backend's own precedent of a manual, not CI-automated, E2E pass).

- [ ] **Step 1: The workflow**

`.github/workflows/frontend-ci.yml`:

```yaml
name: frontend-ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  build-and-lint:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: "npm"
          cache-dependency-path: frontend/package-lock.json
      - run: npm ci
      - run: npm run lint
      - run: npm run build
```

- [ ] **Step 2: Add the lint script (Next.js's built-in ESLint config)**

```bash
cd frontend && npm install -D eslint eslint-config-next
```

In `frontend/package.json`, add to `"scripts"`:

```json
    "lint": "eslint ."
```

Create `frontend/eslint.config.mjs`:

```js
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

export default [...compat.extends("next/core-web-vitals", "next/typescript")];
```

- [ ] **Step 3: Verify locally**

```bash
cd frontend && npm run lint && npm run build
```

Expected: both exit 0. Fix any lint errors or type errors surfaced by `next build`'s stricter production compile before moving on — do not disable rules to force a pass.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/frontend-ci.yml frontend/package.json frontend/package-lock.json frontend/eslint.config.mjs
git commit -m "feat: frontend CI - lint and build on every push/PR"
```

---

### Task 15: Documentation sync + closeout + PR

**Files:**
- Modify: `docs/03-technical-architecture.md` (note Foundation's implementation, if the stack table needs any correction against what was actually built)
- Modify: `docs/superpowers/specs/2026-07-16-frontend-foundation-design.md` (Implementation Status note, matching every prior feature's convention)

**Known gap to record in the closeout note (Task 7 review):** `AuthContext` (Task 6) gives each browser tab its own independent refresh timer with no cross-tab coordination (no `BroadcastChannel`/localStorage leader-election). Two tabs opened close together in time have closely-aligned refresh schedules; if both fire nearly simultaneously, the backend's single-use rotation with family-level reuse detection means the LOSING tab's request is treated as suspected compromise and revokes the entire family — including the winning tab's freshly-issued successor. Net effect: both tabs eventually get logged out, though the winning tab's logout is silent and delayed (up to its next refresh cycle or a reload), not immediate. This is the backend's reuse-detection working exactly as designed against an unsynchronized caller — not a backend defect — but it will read to a real user as an occasional, hard-to-reproduce random session drop when multiple tabs are open. Acceptable for Foundation's scope (no sub-project before this one needed multi-tab session handling), but flag it explicitly as a fast-follow candidate (a `BroadcastChannel`-based single-leader refresh) rather than letting it go undocumented.

**Known gap to record in the closeout note (Task 10 review):** the backend's `POST /auth/register` (`backend/app/routers/auth.py`) returns a distinct `409 "Email already registered"` on a duplicate email, and the frontend's register Route Handler (`frontend/app/(app)/api/auth/register/route.ts`) forwards that message to the client verbatim. This lets anyone enumerate which email addresses already have an account for free — no login attempt or timing analysis needed — which sits at odds with the backend's own care elsewhere (its login endpoint deliberately pays a constant Argon2 cost specifically so login can't be used for the same enumeration). The real fix (a generic backend-side register error, or a distinct non-disclosing code the frontend maps to a generic message) touches `backend/app/routers/auth.py`, which is out of this worktree's scope — flagging as a fast-follow rather than fixing here.

**Known gap to record in the closeout note (Task 12 review):** `AuthContext.clearSession()` (`frontend/contexts/AuthContext.tsx`) calls `clearTimeout(refreshTimerRef.current)`, which only prevents a *not-yet-fired* scheduled refresh — if `scheduleRefresh`'s callback had already begun executing (its `fetch("/api/auth/refresh")` already in flight) in the same narrow window as a `clearSession()` call, that in-flight call's `.then` can still resolve afterward and call `setState(...)`, silently reinstating a stale in-memory session after the caller believed it had logged the user out (e.g., `MfaPanel`'s `disableMfa` flow, or any future explicit-logout path). The window is narrow — the scheduled refresh only fires once per ~14 minutes — and this is a pre-existing `AuthContext` design gap, not something introduced by any specific caller. A proper fix (e.g., a monotonic "session generation" counter that the refresh callback checks before calling `setState`) is `AuthContext`-wide hardening, not scoped to any single Foundation task — flagging as a fast-follow alongside the two-tab refresh race, which shares the same root cause (uncoordinated async refresh completion racing explicit session changes).

- [ ] **Step 1: Full verification pass**

Repeat Task 13 Step 5's live Playwright run once more, from a clean rebuild, to catch anything Tasks 14's lint/build fixes might have disturbed:

```bash
cd frontend && npm run build
```

Expected: exit 0, no type errors. Then the live Docker Compose + Playwright pass exactly as in Task 13 Step 5.

- [ ] **Step 2: Docs sync**

In `docs/03-technical-architecture.md`, near the frontend stack row, note (as a short parenthetical or adjacent line, matching how other docs cross-reference implementation specs elsewhere in this file) that the Foundation layer is implemented per `docs/superpowers/specs/2026-07-16-frontend-foundation-design.md`.

- [ ] **Step 3: Closeout note on the spec**

Add an **Implementation Status** paragraph directly under the spec's title (the established convention every backend feature spec in `docs/superpowers/specs/` uses): completion statement, `next run build`/lint results, the live Playwright pass result, and any deliberately-deferred items surfaced during implementation (e.g., the company-switcher backend gap from the spec's Decision 5, restated here as confirmed-still-open).

- [ ] **Step 4: Commit the closeout**

```bash
git add docs/03-technical-architecture.md docs/superpowers/specs/2026-07-16-frontend-foundation-design.md
git commit -m "docs: close out frontend foundation implementation"
```

- [ ] **Step 5: Push + PR**

```bash
git push -u origin feature/frontend-foundation
```

Write the PR body to a scratch file first (embedded quotes break shell argument quoting — this exact failure has happened before in this project), then:

```bash
gh pr create --base main --head feature/frontend-foundation --title "feat: frontend foundation - auth, session handling, app shell, MFA management" --body-file <scratch-file-path>
```

Confirm CI (both `backend-ci.yml`, unaffected, and the new `frontend-ci.yml`) goes green. **Merging remains an explicit, separate user decision — not automatic**, matching every prior feature in this project.
