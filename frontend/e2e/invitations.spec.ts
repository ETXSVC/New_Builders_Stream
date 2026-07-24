import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

// The backend origin, for the one setup step (minting an invitation) that
// has no UI or BFF route — invitation creation is an API-only admin action
// today, so the spec drives it via Playwright's request API, then covers
// the real user-facing half (the /accept-invitation page) through the UI.
const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";

test("admin invites a teammate who joins via the accept-invitation page", async ({ page, request }) => {
  const uniqueSuffix = randomUUID().slice(0, 8);
  // ".example", not ".test" — see foundation.spec.ts's comment on the
  // backend's EmailStr special-use-domain validation.
  const adminEmail = `e2e-${uniqueSuffix}@invite-admin.example`;
  const inviteeEmail = `e2e-${uniqueSuffix}@invitee.example`;
  const password = "correct-horse-battery-9";

  await test.step("register the admin", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Invite Co ${uniqueSuffix}`);
    await page.getByLabel("Your name").fill("E2E Admin");
    await page.getByLabel("Email").fill(adminEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  let invitationId: string;
  await test.step("mint an invitation via the backend API", async () => {
    const login = await request.post(`${BACKEND_URL}/auth/login`, {
      data: { email: adminEmail, password },
    });
    expect(login.ok()).toBeTruthy();
    const { access_token } = await login.json();
    const invite = await request.post(`${BACKEND_URL}/invitations`, {
      headers: { Authorization: `Bearer ${access_token}` },
      data: { email: inviteeEmail, role: "project_manager" },
    });
    expect(invite.status()).toBe(201);
    invitationId = (await invite.json()).id;
  });

  await test.step("invitee accepts through the page and can log in", async () => {
    // A fresh browser context isn't needed: the accept page is pre-auth
    // and never reads the admin's session. Log the admin out first so the
    // final login lands cleanly.
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/);

    await page.goto(`/accept-invitation?id=${invitationId}`);
    await page.getByLabel("Your name").fill("E2E Invitee");
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByLabel("Confirm password").fill(password);
    await page.getByRole("button", { name: "Accept invitation" }).click();

    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
    await page.getByLabel("Email").fill(inviteeEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  await test.step("a reused invitation link explains itself", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await page.goto(`/accept-invitation?id=${invitationId}`);
    await page.getByLabel("Your name").fill("Second Acceptor");
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByLabel("Confirm password").fill(password);
    await page.getByRole("button", { name: "Accept invitation" }).click();
    // getByText, not getByRole("alert") — Next.js's own route announcer is
    // a second role=alert element and trips strict mode.
    await expect(page.getByText(/already accepted/i)).toBeVisible();
  });
});
