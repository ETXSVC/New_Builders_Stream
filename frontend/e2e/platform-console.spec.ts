import { test, expect } from "@playwright/test";

/**
 * The platform console's boundary, asserted from the browser's side.
 *
 * What this file covers is the half that can be checked WITHOUT a platform
 * administrator account: that the console is closed to anyone who is not
 * signed in, that its BFF refuses to act without the cookie, and that the
 * product's own gate is untouched by the shared `middleware.ts`.
 *
 * The signed-in surface lives in `platform-console-authenticated.spec.ts`
 * and is deliberately a separate file: this one must keep passing with no
 * operator provisioned, since it is the half that proves the console is
 * closed to strangers. That one skips without credentials; this one never
 * should.
 *
 * (For most of this feature's life the signed-in path was untested, on the
 * grounds that reaching it needed a database-owner grant in CI's path and a
 * TOTP library the project does not have. Both were answered rather than
 * argued with once the console grew verbs that create and retire customers
 * — see that file's header.)
 *
 * The redirects below are the load-bearing part: they are what stops
 * `/platform` being world-readable, and nothing else checks them.
 */

const TENANT_ID = "11111111-1111-1111-1111-111111111111";

test.describe("platform console access boundary", () => {
  test("an unauthenticated visitor is sent to the console's own login page", async ({ page }) => {
    await page.goto("/platform");
    await expect(page).toHaveURL(/\/platform\/login$/);
    // The console's login, not the product's — landing on /login would mean
    // an operator authenticating into the wrong trust tier entirely.
    await expect(page.getByRole("heading", { name: "Platform console" })).toBeVisible();
  });

  test("a tenant detail URL is gated too, not just the index", async ({ page }) => {
    await page.goto(`/platform/${TENANT_ID}`);
    await expect(page).toHaveURL(/\/platform\/login$/);
  });

  test("the login page itself stays reachable while signed out", async ({ page }) => {
    await page.goto("/platform/login");
    await expect(page).toHaveURL(/\/platform\/login$/);
    await expect(page.getByLabel("Authenticator code")).toBeVisible();
  });

  test("the console asks for a second factor up front, unlike the product login", async ({
    page,
  }) => {
    await page.goto("/platform/login");
    // Two factors are optional for a tenant user and mandatory here, so there
    // is no code-less first step to reveal — all three fields are present
    // immediately.
    await expect(page.getByLabel("Email")).toBeVisible();
    await expect(page.getByLabel("Password")).toBeVisible();
    await expect(page.getByLabel("Authenticator code")).toBeVisible();
  });

  test("the BFF refuses to act without the console cookie", async ({ request }) => {
    for (const path of [
      "/api/platform/companies",
      `/api/platform/companies/${TENANT_ID}`,
    ]) {
      const response = await request.get(path);
      expect(response.status(), `${path} must not answer unauthenticated`).toBe(401);
    }
  });

  test("a write route refuses without the console cookie", async ({ request }) => {
    const response = await request.patch(`/api/platform/companies/${TENANT_ID}/subscription`, {
      data: { tier: "enterprise" },
    });
    expect(response.status()).toBe(401);
  });

  test("every lifecycle route refuses without the console cookie", async ({ request }) => {
    // These create and retire customers, so "closed to anyone not signed in"
    // is worth asserting per-verb rather than inferring from the subscription
    // route above. A BFF handler that forgot its `platformToken` check would
    // otherwise reach the backend with no credential and 401 there instead —
    // same status, but for the wrong reason and one trust boundary later.
    const create = await request.post("/api/platform/companies", {
      data: {
        company_name: "Should Not Exist",
        owner_email: "nobody@example.com",
        owner_full_name: "Nobody",
      },
    });
    expect(create.status(), "create must not answer unauthenticated").toBe(401);

    const rename = await request.patch(`/api/platform/companies/${TENANT_ID}`, {
      data: { name: "Renamed" },
    });
    expect(rename.status(), "rename must not answer unauthenticated").toBe(401);

    const deactivate = await request.delete(`/api/platform/companies/${TENANT_ID}`);
    expect(deactivate.status(), "deactivate must not answer unauthenticated").toBe(401);

    const restore = await request.post(`/api/platform/companies/${TENANT_ID}/restore`);
    expect(restore.status(), "restore must not answer unauthenticated").toBe(401);
  });

  test("the product's gate still sends its own visitors to the product login", async ({ page }) => {
    // middleware.ts now serves two trust tiers from one function; this is the
    // regression that would catch it routing product traffic into the console.
    await page.goto("/dashboard");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page).not.toHaveURL(/platform/);
  });
});
