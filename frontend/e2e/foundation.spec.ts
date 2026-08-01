import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

test("register, land on dashboard, see MFA nudge on account, log out, log back in", async ({ page }) => {
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

    // Two-factor auth is optional (the forced /account detour was removed
    // when mfa_enrollment_required stopped driving a redirect): every
    // successful registration lands straight on the dashboard.
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  });

  await test.step("account page still nudges MFA enrollment", async () => {
    // mfa_enrollment_required no longer forces a detour, but the account
    // page must still surface the enrollment section for an un-enrolled
    // admin — that's the "optional, not gone" half of the MFA design.
    await page.goto("/account");
    await expect(page.getByRole("heading", { name: "Two-factor authentication" })).toBeVisible();
  });

  await test.step("log out", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/);
  });

  await test.step("the login form offers a way out of a forgotten password", async () => {
    // The whole plumbing in one click-through: page -> BFF -> backend ->
    // 202. What it cannot cover is redeeming the link, because the token
    // exists only in an email and CI has no mailbox to read — the backend
    // suite owns that half (tests/test_password_reset.py).
    // Explicit goto first: the log-out step above ends in a client-side
    // push to /login, and clicking into a second navigation while that one
    // is still settling lets the push land last — putting the browser back
    // on /login with the click apparently ignored.
    await page.goto("/login");
    await page.getByRole("link", { name: "Forgot your password?" }).click();
    await expect(page).toHaveURL(/\/forgot-password/, { timeout: 15_000 });

    // A DELIBERATELY UNREGISTERED address. The confirmation must be the
    // same either way, because a page that says "no account with that
    // email" is an enumeration oracle however careful the API was.
    await page.getByLabel("Email").fill(`nobody-${uniqueSuffix}@nowhere.example`);
    await page.getByRole("button", { name: "Send reset link" }).click();
    await expect(page.getByText(/If that address has an account/)).toBeVisible({
      timeout: 15_000,
    });

    await page.getByRole("link", { name: /Back to log in/ }).click();
    await expect(page).toHaveURL(/\/login/);
  });

  await test.step("log back in", async () => {
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();

    // Same optional-MFA rule on login: straight to the dashboard even
    // though the user never enrolled.
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  });
});
