import { createHmac, randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

/**
 * Review finding M8: five surfaces had no e2e coverage at all — the
 * integrations page, my-tasks, MFA *enrollment* (only the nudge banner
 * was asserted anywhere), branding, and the entire client-role view.
 *
 * Split across two specs by fixture cost rather than by page: everything
 * one admin's own session can reach lives in the first test; the client
 * view needs a second account, a project, and a membership grant, so it
 * gets its own.
 *
 * Same backend-origin escape hatch `invitations.spec.ts` documents: a
 * couple of setup steps (minting an invitation, granting a client access
 * to a project) are API-only admin actions with no UI today. They are
 * driven through the request API so the spec can then cover the
 * user-facing half through the browser.
 */
const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";

/**
 * RFC 6238 TOTP over the base32 secret the enrollment step displays.
 *
 * Written out rather than pulled from npm because it is ~20 lines against
 * `node:crypto`, and this is the assertion that makes the MFA test worth
 * having: anything that merely typed "123456" would prove the form posts,
 * not that the secret on screen is the one the backend enrolled. A wrong
 * secret, a wrong digest, or a wrong time step all fail here.
 */
function currentTimeStep(): number {
  return Math.floor(Date.now() / 1000 / 30);
}

/**
 * Block until the TOTP time step advances.
 *
 * The backend's replay guard (app/services/mfa.py) refuses any code at or
 * before `totp_last_used_step` — correctly, since a captured code must not
 * be reusable. Enrolment and the re-login below otherwise land in the same
 * 30s window and produce the SAME six digits, so the login is rejected
 * with "Invalid TOTP code" and the test fails on a working product.
 *
 * Costs up to 30s once per run. The alternative — asserting the login
 * challenge merely appears — would drop the only assertion proving the
 * enrolled secret is the one the backend stored.
 */
async function waitForNextTimeStep(afterStep: number): Promise<void> {
  while (currentTimeStep() <= afterStep) {
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
}

function totp(base32Secret: string): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of base32Secret.replace(/=+$/, "").toUpperCase()) {
    const index = alphabet.indexOf(char);
    if (index < 0) continue;
    bits += index.toString(2).padStart(5, "0");
  }
  const bytes = Buffer.from(
    (bits.match(/.{8}/g) ?? []).map((byte) => parseInt(byte, 2))
  );

  const counter = currentTimeStep();
  const counterBytes = Buffer.alloc(8);
  counterBytes.writeBigUInt64BE(BigInt(counter));

  const digest = createHmac("sha1", bytes).update(counterBytes).digest();
  // Dynamic truncation, per the RFC: the low nibble of the last byte
  // picks the 4-byte window, and the top bit is masked off so the result
  // is unsigned.
  const offset = digest[digest.length - 1] & 0x0f;
  const binary =
    ((digest[offset] & 0x7f) << 24) |
    (digest[offset + 1] << 16) |
    (digest[offset + 2] << 8) |
    digest[offset + 3];
  return (binary % 1_000_000).toString().padStart(6, "0");
}

test("an admin enrols in MFA, reads their tasks, and reaches integrations and branding", async ({
  page,
}) => {
  // Same reasoning as crm-pm.spec's own override: this arc is the first
  // hit on several Route Handlers, and `next dev` compiles them on demand.
  test.setTimeout(180_000);
  const suffix = randomUUID().slice(0, 8);
  const email = `e2e-account-${suffix}@foundation.example`;
  const password = "correct-horse-battery-9";

  await test.step("register", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Account Co ${suffix}`);
    await page.getByLabel("Your name").fill("E2E Account Tester");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  await test.step("my-tasks renders its empty state for a fresh account", async () => {
    // The empty state is the assertion worth making here: a brand-new
    // admin genuinely has no assigned tasks, and a page that threw or
    // rendered a bare skeleton would look identical to "no tasks" to any
    // check less specific than this one.
    await page.getByRole("link", { name: "Account" }).click();
    await page.goto("/my-tasks");
    await expect(page.getByRole("heading", { name: "My tasks" })).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText("No tasks assigned to you right now.")).toBeVisible();
  });

  await test.step("integrations lists both providers as not connected", async () => {
    await page.getByRole("link", { name: "Integrations" }).click();
    await expect(page.getByRole("heading", { name: "Integrations" })).toBeVisible({
      timeout: 15_000,
    });
    // Deliberately NOT clicking Connect: the OAuth handshake leaves the
    // app for a third-party consent screen that does not exist in CI, and
    // the fake provider clients this build ships never move real money.
    // What is worth asserting is that the page resolves each provider's
    // connection state rather than erroring — which it can only do by
    // actually reaching the backend.
    await expect(page.getByRole("button", { name: /Connect QuickBooks/ })).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByRole("button", { name: /Connect FreshBooks/ })).toBeVisible();
  });

  await test.step("branding is reachable and saves", async () => {
    await page.getByRole("link", { name: "Catalog" }).click();
    await page.getByRole("tab", { name: "PDF template" }).click();
    await expect(page.getByLabel("Footer / terms text")).toBeVisible({ timeout: 15_000 });
    await page.getByLabel("Footer / terms text").fill(`Terms ${suffix}`);
    // The name outbound email goes out under (migration 0027) — same form,
    // because it is the same decision: what this company looks like from
    // outside. Blank would mean "send as the company name".
    await page.getByLabel("Email sender name").fill(`Sender ${suffix}`);
    await page.getByRole("button", { name: /Save/ }).click();
    // Round-trip through a reload rather than trusting the in-page
    // success state: the point is that the value persisted, not that the
    // form said it did.
    await page.reload();
    await page.getByRole("tab", { name: "PDF template" }).click();
    await expect(page.getByLabel("Footer / terms text")).toHaveValue(`Terms ${suffix}`, {
      timeout: 15_000,
    });
    await expect(page.getByLabel("Email sender name")).toHaveValue(`Sender ${suffix}`);
  });

  // Hoisted out of the step below so the login step can recompute a FRESH
  // code from the same secret — TOTP codes expire on a 30s step, so
  // reusing the enrolment code would fail intermittently depending on
  // where in that window the enrolment landed.
  let secret = "";
  let enrolmentStep = 0;

  await test.step("enrol in MFA with a real TOTP code", async () => {
    await page.getByRole("link", { name: "Account" }).click();
    // getByRole, not getByText: the page also carries the admin nudge
    // banner and the Enable button, both of which contain this phrase.
    await expect(
      page.getByRole("heading", { name: "Two-factor authentication" })
    ).toBeVisible({ timeout: 15_000 });

    await page.getByRole("button", { name: "Enable two-factor authentication" }).click();
    // The base32 secret is rendered for manual entry (no QR in this
    // build — see MfaPanel's comment).
    secret = (await page.locator("code").first().innerText()).trim();
    expect(secret.length).toBeGreaterThan(0);

    enrolmentStep = currentTimeStep();
    // `exact`, because accessible-name matching is substring by default and
    // this page now also carries a "Postal code" field (the directory panel).
    // Without it this resolves to two elements and fails strict mode.
    await page.getByLabel("Code", { exact: true }).fill(totp(secret));
    await page.getByRole("button", { name: "Confirm" }).click();

    // Enrolment succeeded iff the panel now offers the DISABLE form —
    // which asks for the current password, a field that does not exist in
    // the not-yet-enrolled state.
    await expect(page.getByLabel("Current password")).toBeVisible({ timeout: 15_000 });
    await expect(
      page.getByRole("button", { name: "Enable two-factor authentication" })
    ).toBeHidden();
  });

  await test.step("logging back in now demands the second factor", async () => {
    // The half that actually matters: enrolment that does not change the
    // login path has protected nothing. A stale-secret or
    // never-persisted enrolment passes the step above and fails here.
    await waitForNextTimeStep(enrolmentStep);
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: /Log in|Sign in/ }).click();

    const code = page.getByLabel("Authenticator code");
    await expect(code).toBeVisible({ timeout: 15_000 });
    await code.fill(totp(secret));
    await page.getByRole("button", { name: "Verify" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });
});

test("a client sees only the project they were granted, and sees it sanitized", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const suffix = randomUUID().slice(0, 8);
  const adminEmail = `e2e-cadmin-${suffix}@foundation.example`;
  const clientEmail = `e2e-client-${suffix}@customer.example`;
  const password = "correct-horse-battery-9";

  let adminToken = "";
  let grantedProjectId = "";
  let otherProjectId = "";
  let invitationId = "";

  await test.step("register the admin and set up two projects", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Client Co ${suffix}`);
    await page.getByLabel("Your name").fill("E2E Client Admin");
    await page.getByLabel("Email").fill(adminEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });

    const login = await request.post(`${BACKEND_URL}/auth/login`, {
      data: { email: adminEmail, password },
    });
    expect(login.ok()).toBeTruthy();
    adminToken = (await login.json()).access_token;
    const auth = { Authorization: `Bearer ${adminToken}` };

    // TWO projects in the SAME company. That is the whole point: RLS is
    // company-scoped and says nothing about two customers of one builder,
    // so a single-project fixture would pass against the vulnerable code.
    for (const name of [`Granted ${suffix}`, `Withheld ${suffix}`]) {
      const created = await request.post(`${BACKEND_URL}/projects`, {
        headers: auth,
        data: { name, site_address: "1 Test Way" },
      });
      expect(created.status()).toBe(201);
      const { id } = await created.json();
      if (name.startsWith("Granted")) grantedProjectId = id;
      else otherProjectId = id;
    }

    const invite = await request.post(`${BACKEND_URL}/invitations`, {
      headers: auth,
      data: { email: clientEmail, role: "client" },
    });
    expect(invite.status()).toBe(201);
    invitationId = (await invite.json()).id;
  });

  await test.step("the client joins and is granted access to exactly one project", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await page.goto(`/accept-invitation?id=${invitationId}`);
    await page.getByLabel("Your name").fill("E2E Customer");
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByLabel("Confirm password").fill(password);
    await page.getByRole("button", { name: "Accept invitation" }).click();
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });

    // The membership grant (migration 0019's project_clients row) is an
    // API-only admin action today — same escape hatch the invitation
    // minting above uses.
    const clientUser = await request.get(`${BACKEND_URL}/companies/members`, {
      headers: { Authorization: `Bearer ${adminToken}` },
    });
    expect(clientUser.ok()).toBeTruthy();
    const member = (await clientUser.json()).items.find(
      (m: { role: string }) => m.role === "client"
    );
    expect(member).toBeTruthy();

    const grant = await request.post(`${BACKEND_URL}/projects/${grantedProjectId}/clients`, {
      headers: { Authorization: `Bearer ${adminToken}` },
      data: { user_id: member.user_id },
    });
    expect(grant.status()).toBe(201);
  });

  await test.step("the granted project renders the sanitized client dashboard", async () => {
    await page.goto("/login");
    await page.getByLabel("Email").fill(clientEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: /Log in|Sign in/ }).click();
    // `/projects`, not `/dashboard`. LoginForm pushes /dashboard for
    // everyone, but app/(app)/dashboard/page.tsx immediately
    // `router.replace`s a `client` to `/projects/{first project}` (or
    // `/projects` when that lookup has not resolved yet) — a client has
    // no dashboard of their own.
    //
    // So /dashboard is a TRANSIENT url for this role, and asserting it
    // was a race on whether Playwright sampled before or after the
    // replace: it passed locally and on the PR run, then flaked on main.
    // The retry passing was luck, not evidence. This prefix matches both
    // landing spots and is stable.
    await expect(page).toHaveURL(/\/projects/, { timeout: 15_000 });

    await page.goto(`/projects/${grantedProjectId}`);
    await expect(page.getByText(`Granted ${suffix}`)).toBeVisible({ timeout: 15_000 });
    // The client shape carries derived counts and no staff machinery —
    // design decision #8. Asserting a tab is ABSENT is what distinguishes
    // "the sanitized dashboard rendered" from "the staff page rendered
    // and happened to show the name".
    await expect(page.getByText(/0 of 0 tasks complete/)).toBeVisible();
    await expect(page.getByRole("tab", { name: "Phases & tasks" })).toBeHidden();
  });

  await test.step("the withheld project is a 404, not a 403", async () => {
    // Migration 0019's rule, and the reason it raises 404: a client must
    // not be able to tell "this project exists but isn't yours" apart
    // from "no such project", or the ids of other customers' work become
    // enumerable.
    await page.goto(`/projects/${otherProjectId}`);
    await expect(page.getByText(`Withheld ${suffix}`)).toBeHidden({ timeout: 15_000 });
    await expect(page.getByRole("alert")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/not found/i)).toBeVisible();
  });
});
