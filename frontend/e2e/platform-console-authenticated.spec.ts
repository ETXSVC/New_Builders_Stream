import { test, expect, type BrowserContext, type Page } from "@playwright/test";
import { secondsLeftInStep, totpCode } from "./helpers/totp";

/** One entry of `BrowserContext.cookies()`, which Playwright does not export. */
type Cookie = Awaited<ReturnType<BrowserContext["cookies"]>>[number];

/**
 * The platform console's SIGNED-IN surface, driven through the browser.
 *
 * This is the half `platform-console.spec.ts` deliberately left uncovered.
 * The reasoning there was that reaching it needs a privilege-granting
 * script in CI's path and a TOTP library the project does not have. Both
 * held at the time; neither survives contact with what the console can now
 * do. Since then it creates customers and takes them out of service, and
 * "the backend suite asserts the API" stops being a satisfying answer for
 * verbs of that weight — a modal that posts the wrong field, or a button
 * wired to the wrong id, is invisible to `test_platform_admin.py` and very
 * visible to whoever clicks it.
 *
 * The two objections were answered rather than argued with:
 *
 *   * `backend/scripts/provision_e2e_operator.py` does the grant and the
 *     MFA enrollment, because it is the side of the fence with database
 *     access. It runs the REAL `grant_platform_admin.py`, so the path
 *     operators are told to use is the path under test.
 *   * `helpers/totp.ts` is RFC 6238 over `node:crypto` — no dependency
 *     added, and it is cross-checked against the backend's `pyotp` by
 *     virtue of the codes being accepted at all.
 *
 * The spec still signs in properly, through `POST /api/platform/auth/login`
 * with a real code. Handing it a pre-minted token would have been simpler
 * and would have proven nothing about the login it skipped — the cookie
 * this puts in the browser context is the thing everything else depends on.
 *
 * SKIPPED, NOT FAILED, when the credentials are absent. A developer running
 * `npm run test:e2e` against a bare stack has not provisioned an operator,
 * and turning that into a red suite would train people to ignore it. CI
 * always provisions, so the coverage is never silently lost there.
 */

const EMAIL = process.env.E2E_PLATFORM_EMAIL;
const PASSWORD = process.env.E2E_PLATFORM_PASSWORD;
const SECRET = process.env.E2E_PLATFORM_TOTP_SECRET;

// Unique per run so a re-run never collides with its own leftovers, and
// prefixed `E2E ` so `backend/scripts/prune_dev_tenants.py` can find what
// this leaves in a dev database.
const RUN = Date.now().toString().slice(-8);

test.describe("platform console, signed in", () => {
  test.skip(
    !EMAIL || !PASSWORD || !SECRET,
    "No operator provisioned — run backend/scripts/provision_e2e_operator.py and export its output."
  );

  // SERIAL, and sign in exactly once.
  //
  // Not a performance choice. `verify_totp_code`'s replay guard refuses any
  // timestep at or BEFORE the last one used successfully, so a per-test
  // sign-in has every test after the first landing in the same 30-second
  // window and being rejected as a replay — which is the guard working,
  // and looked exactly like a flaky login until the codes were lined up
  // against the clock. Sleeping 30s per test would "fix" it and cost
  // minutes; reusing the session costs nothing and is closer to what an
  // operator does anyway.
  //
  // The token is a stateless JWT with a 30-minute life and no server-side
  // revocation, so the captured cookie stays good for the whole file — the
  // sign-out test clears it from ITS context only, which is why that test
  // does not poison the ones after it.
  test.describe.configure({ mode: "serial" });

  let sessionCookie: Cookie | null = null;

  /**
   * Signs in through the console's own login form.
   *
   * Through the FORM rather than by posting to the BFF, because the form is
   * part of what is being tested: three fields asked for up front, and a
   * successful submit landing on the tenant list. Everything after this
   * depends on the httpOnly cookie the BFF sets, so if this is wrong
   * nothing else in the file means anything.
   */
  async function signIn(page: Page) {
    await page.goto("/platform/login");
    await page.getByLabel("Email").fill(EMAIL!);
    await page.getByLabel("Password").fill(PASSWORD!);

    // Don't submit a code computed in the dying seconds of a timestep: the
    // backend would verify it in the next one. Cheaper than a retry loop
    // and removes a once-in-a-while failure that reads as a real bug.
    if (secondsLeftInStep() < 5) await page.waitForTimeout(5_000);
    await page.getByLabel("Authenticator code").fill(totpCode(SECRET!));

    await page.getByRole("button", { name: "Sign in" }).click();

    // One retry, in a LATER timestep, before giving up.
    //
    // This is the replay guard again, seen from the other side. A Playwright
    // retry runs in a fresh worker process, so nothing in this file can
    // remember which code the previous attempt burned — and the retry starts
    // seconds after the failure, i.e. usually inside the very timestep the
    // first run signed in with. The login is then rejected as a replay and
    // the report blames the login for whatever actually failed, which is
    // exactly how a missing PLATFORM_DATABASE_URL first showed up as "the
    // operator cannot sign in". Waiting out the window costs 30s on a retry
    // and nothing at all on a green run.
    if (!(await landedOnConsole(page, 5_000))) {
      await page.waitForTimeout((secondsLeftInStep() + 1) * 1_000);
      await page.getByLabel("Authenticator code").fill(totpCode(SECRET!));
      await page.getByRole("button", { name: "Sign in" }).click();
    }

    await expect(page).toHaveURL(/\/platform$/, { timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "Tenants" })).toBeVisible();
  }

  /** Did the submit land on the tenant list within `timeout`? */
  async function landedOnConsole(page: Page, timeout: number): Promise<boolean> {
    try {
      await expect(page).toHaveURL(/\/platform$/, { timeout });
      return true;
    } catch {
      return false;
    }
  }

  test.beforeEach(async ({ page, context }) => {
    if (sessionCookie) {
      await context.addCookies([sessionCookie]);
      await page.goto("/platform");
      return;
    }
    await signIn(page);
    sessionCookie = (await context.cookies()).find((c) => c.name === "platform_token") ?? null;
    expect(sessionCookie, "login did not set the console cookie").not.toBeNull();
  });

  test("an operator signs in with email, password and a live TOTP code", async ({ page }) => {
    // The form flow itself, run once. Everything after this reuses the
    // cookie it produced, so this is the test that actually covers
    // /platform/login — and it must come first (serial mode above).
    await expect(page.getByRole("heading", { name: "Tenants" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  });

  test("the tenant list renders real tenants", async ({ page }) => {
    // The operator's own company exists because provisioning registered it,
    // so there is always at least one row — an empty list here would mean
    // the cursor loader or its auth wiring is broken, not that the database
    // is empty.
    await expect(page.getByRole("link", { name: "E2E Operator Co" })).toBeVisible();
    await expect(page.getByRole("button", { name: "New tenant" })).toBeVisible();
  });

  test("creating a tenant shows its one-time password and lists it", async ({ page }) => {
    const name = `E2E Created Co ${RUN}`;

    await page.getByRole("button", { name: "New tenant" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Company name").fill(name);
    await dialog.getByLabel("Owner name").fill("Created Owner");
    await dialog.getByLabel("Owner email").fill(`e2e-created-${RUN}@platform.example`);
    await dialog.getByLabel("Tier").selectOption("enterprise");
    await dialog.getByRole("button", { name: "Create tenant" }).click();

    // The second screen is the reason this is a modal at all: the password
    // is returned once and is not recoverable, so it must not vanish on its
    // own. Assert it is on screen and labelled as unrepeatable.
    await expect(page.getByText(`${name} is live`)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("This is shown once")).toBeVisible();
    const password = await page.locator("code").first().innerText();
    expect(password.length).toBeGreaterThan(20);

    await page.getByRole("button", { name: "Done" }).click();
    await expect(page.getByRole("link", { name })).toBeVisible({ timeout: 15_000 });
  });

  test("the quick-edit modal changes a tier", async ({ page }) => {
    const name = `E2E Tier Co ${RUN}`;
    await createTenant(page, name, `e2e-tier-${RUN}@platform.example`, "starter");

    await page
      .getByRole("row", { name: new RegExp(name) })
      .getByRole("button", { name: "Edit" })
      .click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Tier").selectOption("pro");
    await dialog.getByRole("button", { name: "Save" }).click();

    // Back on the list, re-read from the server rather than trusting the
    // modal's own optimism.
    await expect(
      page.getByRole("row", { name: new RegExp(name) }).getByText("pro", { exact: true })
    ).toBeVisible({ timeout: 15_000 });
  });

  test("a module override survives a round trip, and clearing it reverts to the tier", async ({
    page,
  }) => {
    // Worth having in a BROWSER test specifically: this route (two dynamic
    // segments) is the one Turbopack intermittently fails to register under
    // `next dev`. CI serves a production build, where it is fine — so this
    // assertion covers a path local development cannot always reach.
    const name = `E2E Override Co ${RUN}`;
    await createTenant(page, name, `e2e-override-${RUN}@platform.example`, "starter");
    await page.getByRole("link", { name }).click();

    const estimation = page.getByRole("row", { name: /estimation/ });
    // `starter` does not include estimation, so the tier alone says no.
    await expect(estimation.getByText("not included")).toBeVisible({ timeout: 15_000 });

    await estimation.getByRole("button", { name: "Grant" }).click();
    await expect(estimation.getByText("granted")).toBeVisible({ timeout: 15_000 });
    await expect(estimation.getByText("on", { exact: true })).toBeVisible();

    await estimation.getByRole("button", { name: "Follow tier" }).click();
    await expect(estimation.getByText("none")).toBeVisible({ timeout: 15_000 });
    await expect(estimation.getByText("off", { exact: true })).toBeVisible();
  });

  test("taking a tenant out of service is reversible from the UI", async ({ page }) => {
    const name = `E2E Retired Co ${RUN}`;
    await createTenant(page, name, `e2e-retired-${RUN}@platform.example`, "pro");
    await page.getByRole("link", { name }).click();

    await page.getByRole("button", { name: "Take out of service" }).click();
    // The house ConfirmButton arms in place rather than opening a dialog.
    await page.getByRole("button", { name: "Take out of service" }).last().click();

    await expect(page.getByText(/Out of service/)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("Nothing was deleted.")).toBeVisible();

    await page.getByRole("button", { name: "Restore to service" }).click();
    await expect(page.getByRole("button", { name: "Take out of service" })).toBeVisible({
      timeout: 15_000,
    });
  });

  test("signing out ends the session", async ({ page }) => {
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/platform\/login$/, { timeout: 15_000 });

    // And the cookie is really gone — a soft redirect that left it in place
    // would let a back-button navigation straight back in.
    await page.goto("/platform");
    await expect(page).toHaveURL(/\/platform\/login$/);
  });
});

/** Create a tenant through the modal and dismiss the credential screen. */
async function createTenant(page: Page, name: string, email: string, tier: string) {
  await page.getByRole("button", { name: "New tenant" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Company name").fill(name);
  await dialog.getByLabel("Owner name").fill("Owner");
  await dialog.getByLabel("Owner email").fill(email);
  await dialog.getByLabel("Tier").selectOption(tier);
  await dialog.getByRole("button", { name: "Create tenant" }).click();
  await expect(page.getByText(`${name} is live`)).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByRole("link", { name })).toBeVisible({ timeout: 15_000 });
}
