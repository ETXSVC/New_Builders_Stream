import { test, expect } from "@playwright/test";

test("register, land on account (MFA nudge), reach dashboard, log out, log back in", async ({ page }) => {
  const uniqueSuffix = Date.now().toString();
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

  await page.goto("/register");
  await page.getByLabel("Company name").fill(`E2E Foundation Co ${uniqueSuffix}`);
  await page.getByLabel("Your name").fill("E2E Tester");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Create account" }).click();

  // A freshly-registered admin always has mfa_enrollment_required=true
  // (backend/app/routers/auth.py — deliberate, Task 7's MFA design), so
  // registration lands on /account, not /dashboard.
  await expect(page).toHaveURL(/\/account/);
  await expect(page.getByRole("heading", { name: "Two-factor authentication" })).toBeVisible();

  // The dashboard shell itself is reachable and renders correctly with a
  // live session — middleware only requires a valid refresh_token cookie,
  // which register's auto-login already established.
  await page.goto("/dashboard");
  await expect(page.getByText("Welcome")).toBeVisible();

  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL(/\/login/);

  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Log in" }).click();

  // MFA still isn't enrolled, so re-login lands on /account again — this
  // is deterministic, not flaky: the same backend rule applies every time.
  await expect(page).toHaveURL(/\/account/);
  await expect(page.getByRole("heading", { name: "Two-factor authentication" })).toBeVisible();
});
