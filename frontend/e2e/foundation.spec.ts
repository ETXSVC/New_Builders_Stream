import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

test("register, land on account (MFA nudge), reach dashboard, log out, log back in", async ({ page }) => {
  const uniqueSuffix = randomUUID().slice(0, 8);
  // ".example" (not ".test") — the live backend's EmailStr validation
  // (email_validator, no `test_environment` override outside the pytest
  // suite — see backend/tests/conftest.py's own comment on this) rejects
  // any ".test" TLD as an RFC 2606 special-use domain. ".example" is not
  // in email_validator's SPECIAL_USE_DOMAIN_NAMES denylist (confirmed:
  // ['arpa', 'invalid', 'local', 'localhost', 'onion', 'test']) and is
  // itself an RFC 2606 reserved documentation domain, so it's the
  // correct choice for a fake E2E address that must pass real validation.
  const email = `e2e-${uniqueSuffix}@foundation.example`;
  const password = "correct-horse-battery-9";

  await test.step("register", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Foundation Co ${uniqueSuffix}`);
    await page.getByLabel("Your name").fill("E2E Tester");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();

    // A freshly-registered admin always has mfa_enrollment_required=true
    // (backend/app/routers/auth.py — deliberate, Task 7's MFA design), so
    // registration lands on /account, not /dashboard.
    await expect(page).toHaveURL(/\/account/, { timeout: 15_000 });
    await expect(page.getByRole("heading", { name: "Two-factor authentication" })).toBeVisible();
  });

  await test.step("dashboard shell is reachable", async () => {
    // Proves the route compiles/renders and middleware's refresh_token
    // cookie gate passes — NOT a guarantee that AuthContext's client-side
    // hydration produced a confirmed session (DashboardPage renders its
    // "Welcome" card unconditionally regardless of accessToken; see
    // dashboard/page.tsx). A real hydration failure wouldn't fail this
    // assertion. Known further gap, documented in the plan's Task 15
    // closeout section: React Strict Mode (default-on, next.config.ts has
    // no override) double-invokes AuthContext's mount effect in `next dev`,
        // which can fire two /api/auth/refresh calls against a single-use
    // rotating token — the same reuse-detection race already documented
    // for multi-tab sessions, but reachable here within one tab/dev-only.
    await page.goto("/dashboard");
    await expect(page.getByRole("heading", { name: "Welcome" })).toBeVisible();
  });

  await test.step("log out", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/);
  });

  await test.step("log back in", async () => {
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();

    // MFA still isn't enrolled, so re-login lands on /account again — this
    // is deterministic, not flaky: the same backend rule applies every time.
    await expect(page).toHaveURL(/\/account/, { timeout: 15_000 });
    await expect(page.getByRole("heading", { name: "Two-factor authentication" })).toBeVisible();
  });
});
