import { randomUUID } from "node:crypto";
import { test, expect, request as playwrightRequest, type Page } from "@playwright/test";

const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";
const PASSWORD = "correct-horse-battery-9";

/**
 * Offline estimate capture, end to end.
 *
 * The claim under test is the one the whole feature rests on and the one no
 * unit test can make: **the app cold-starts with no network at all** — a new
 * document load, not a tab that was already open — and the estimator can
 * work. That is a service worker replaying a cached response whose CSP
 * header and whose script nonces were minted together, so they still agree;
 * get the caching subtly wrong and the page fails SILENTLY, rendering markup
 * that never hydrates.
 *
 * So both tests below assert two things a screenshot could not distinguish:
 * that a **controlled React input accepts typing** (the JavaScript ran), and
 * that **no CSP violation was logged** (it ran under the cached policy
 * rather than in spite of it).
 *
 * MUST RUN AGAINST A PRODUCTION BUILD. `next dev` adds `'unsafe-eval'` to
 * `script-src`, which would mask exactly the failure being looked for. CI
 * builds and `next start`s the frontend, so this holds there.
 */

async function registerCompany(page: Page, suffix: string): Promise<string> {
  const email = `e2e-offline-${suffix}@foundation.example`;
  await page.goto("/register");
  await page.getByLabel("Company name").fill(`E2E Offline Co ${suffix}`);
  await page.getByLabel("Your name").fill("E2E Offline Tester");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  return email;
}

/** A catalog item, a markup profile and a project — the three things a
 *  captured estimate refers to, none of which can be created offline. */
async function seedEstimateInputs(page: Page, suffix: string): Promise<void> {
  await page.getByRole("link", { name: "Catalog", exact: true }).click();
  await page.getByLabel("Category").fill("Framing");
  await page.getByLabel("Name", { exact: true }).fill("Lumber");
  await page.getByLabel("Unit", { exact: true }).fill("bf");
  await page.getByLabel("Unit rate").fill("4.00");
  await page.getByRole("button", { name: "Add item" }).click();
  await expect(page.getByText("Lumber")).toBeVisible({ timeout: 15_000 });

  await page.getByRole("tab", { name: "Markup profiles" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Standard");
  await page.getByLabel("Overhead %").fill("10");
  await page.getByLabel("Profit %").fill("15");
  await page.getByRole("button", { name: "Add profile" }).click();
  await expect(page.getByText("Standard")).toBeVisible({ timeout: 15_000 });

  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await page.getByRole("link", { name: "New project" }).click();
  await page.getByLabel("Project name").fill(`Deck ${suffix}`);
  await page.getByLabel("Site address").fill("1 Main St");
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByRole("heading", { name: `Deck ${suffix}` })).toBeVisible({
    timeout: 15_000,
  });
}

async function primeForOffline(page: Page): Promise<void> {
  await page.goto("/estimates/capture");
  await page.getByRole("button", { name: "Make available offline" }).click();
  await expect(page.getByText("Ready to work offline.")).toBeVisible({ timeout: 30_000 });
  // The worker must be CONTROLLING this page, not merely registered: an
  // active worker that has not claimed the client caches nothing, and the
  // offline reload below would fail for a reason that looks like the
  // feature not working.
  await page.waitForFunction(() => !!navigator.serviceWorker.controller, null, {
    timeout: 15_000,
  });
}

/** Capture one line against the seeded project and save it locally. */
async function captureDraft(page: Page, suffix: string): Promise<void> {
  await page.getByLabel("For").selectOption({ label: `Project: Deck ${suffix}` });
  await page.getByLabel("Markup profile").selectOption({ label: "Standard" });
  await page.getByRole("button", { name: "+" }).first().click();
  // Typing into a controlled React input is the load-bearing assertion of
  // this whole spec when it runs offline: markup alone would render without
  // hydration, and this only works if the JavaScript actually ran under the
  // cached page's own CSP.
  await page.getByLabel(/Quantity for Lumber/).fill("10");
  await expect(page.getByLabel(/Quantity for Lumber/)).toHaveValue("10");
  await page.getByRole("button", { name: "Save draft" }).click();
  // By role, not by text: `getByText` matches case-insensitive
  // SUBSTRINGS, so "Saved on this device" also matches the offline badge
  // ("...drafts are saved on this device") and fails strict mode.
  await expect(page.getByRole("heading", { name: "Saved on this device" })).toBeVisible({
    timeout: 15_000,
  });
}

async function repriceLumberOutOfBand(email: string, unitRate: string): Promise<void> {
  const api = await playwrightRequest.newContext({ baseURL: BACKEND_URL });
  const login = await api.post("/auth/login", { data: { email, password: PASSWORD } });
  expect(login.ok()).toBeTruthy();
  const token = (await login.json()).access_token;

  const items = await api.get("/catalogs/items", { headers: { Authorization: `Bearer ${token}` } });
  expect(items.ok()).toBeTruthy();
  const lumber = (await items.json()).items.find((i: { name: string }) => i.name === "Lumber");
  expect(lumber, "seeded catalog item should be listed").toBeTruthy();

  const patched = await api.patch(`/catalogs/items/${lumber.id}`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { unit_rate: unitRate },
  });
  expect(patched.ok()).toBeTruthy();
  await api.dispose();
}

test("offline capture: cold-starts with no network, and flushes when it returns", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const suffix = randomUUID().slice(0, 8);

  await test.step("register, seed, and prime the device", async () => {
    await registerCompany(page, suffix);
    await seedEstimateInputs(page, suffix);
    await primeForOffline(page);
  });

  await test.step("control: a page the worker does not cache cannot load offline", async () => {
    await context.setOffline(true);
    // Without this the test below proves only that A page rendered — not
    // that the worker is why, and not that the allowlist in `sw.js` is real
    // rather than "cache whatever was visited."
    //
    // On its OWN page, sharing the context (and so the worker and the
    // caches) but not the tab: Chromium commits its error page
    // asynchronously after `goto` rejects, and that commit lands in the
    // middle of whatever navigation comes next — "interrupted by another
    // navigation to chrome-error://chromewebdata/", which reads as the
    // cached page failing when it is this step's litter.
    const control = await context.newPage();
    let failed = false;
    try {
      await control.goto("/estimates", { timeout: 15_000 });
    } catch {
      failed = true;
    }
    await control.close();
    expect(failed, "/estimates is not in the worker's allowlist and must not load offline").toBe(
      true
    );
  });

  const cspViolations: string[] = [];
  page.on("console", (message) => {
    if (/content security policy/i.test(message.text())) cspViolations.push(message.text());
  });

  await test.step("cold-start the capture screen with no network", async () => {
    await page.goto("/estimates/capture");
    await expect(page.getByRole("heading", { name: "On-site capture" })).toBeVisible({
      timeout: 15_000,
    });
    // The exact badge, not /Offline/i — this screen says the word "offline"
    // in four other places, and a loose matcher would resolve to five
    // elements and fail strict mode rather than assert anything.
    await expect(page.getByText("Offline — drafts are saved on this device")).toBeVisible();
    // Read out of IndexedDB, not the network: the catalog is the reason
    // this screen is worth caching at all.
    await expect(page.getByText(/Lumber/)).toBeVisible({ timeout: 15_000 });
  });

  await test.step("capture an estimate with no network", async () => {
    await captureDraft(page, suffix);
    await expect(page.getByText(/Held until you are back in signal/)).toBeVisible();
  });

  await test.step("the cached page hydrated under its own CSP", async () => {
    // Asserted after the interactions above, so it covers the whole offline
    // session rather than just the first paint.
    expect(cspViolations, "a cached document must not violate its cached policy").toEqual([]);
  });

  await test.step("connectivity returns, and the draft sends itself", async () => {
    await context.setOffline(false);
    await expect(page.getByRole("heading", { name: "Sent" })).toBeVisible({ timeout: 60_000 });
    // 10 x $4.00, +10% overhead, +15% profit = $50.60 — calculated by
    // `POST /estimates/{id}/calculate`, which is the step the client
    // deliberately does not reimplement. A client-side total that rounded
    // at a different stage would disagree by cents on a document a
    // customer signs.
    await expect(page.getByText("$50.60")).toBeVisible();
    // Nothing left waiting: the draft is deleted once the server holds it.
    await expect(page.getByRole("heading", { name: "Saved on this device" })).toHaveCount(0);
  });

  await test.step("the estimate really exists on the server", async () => {
    await page.getByRole("link", { name: "Open it" }).click();
    await expect(page).toHaveURL(/\/estimates\/[0-9a-f-]+$/, { timeout: 15_000 });
    // The PDF button is gated on `estimate.total !== null`, so its presence
    // is the detail screen's own statement that all three calls landed.
    await expect(page.getByRole("button", { name: "Generate PDF" })).toBeVisible({
      timeout: 15_000,
    });
  });
});

test("a rate that moved while the estimator was offline parks the draft", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const suffix = randomUUID().slice(0, 8);
  let email = "";

  await test.step("register, seed, and prime the device", async () => {
    email = await registerCompany(page, suffix);
    await seedEstimateInputs(page, suffix);
    await primeForOffline(page);
  });

  await test.step("capture a line at the rate the estimator can see", async () => {
    await context.setOffline(true);
    await page.goto("/estimates/capture");
    await expect(page.getByRole("heading", { name: "On-site capture" })).toBeVisible({
      timeout: 15_000,
    });
    await captureDraft(page, suffix);
  });

  await test.step("a colleague reprices the catalog while they are out of signal", async () => {
    // Through the API, from a separate request context — the browser is
    // offline, the rest of the world is not. This is the scenario the whole
    // guard exists for: capture and write are days apart.
    await repriceLumberOutOfBand(email, "9.00");
  });

  await test.step("the flush refuses, parks the draft, and says what moved", async () => {
    await context.setOffline(false);
    await expect(page.getByRole("heading", { name: "Needs your attention" })).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByText(/rate\(s\) changed/i)).toBeVisible();
    // Both rates, precisely — whole-dollar rounding would render a 4.00 ->
    // 4.05 conflict as "$4 -> $4", which is worse than saying nothing.
    await expect(page.getByText("$4.00", { exact: true })).toBeVisible();
    await expect(page.getByText("$9.00", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Use new rates" })).toBeVisible();
    // The point of parking rather than retrying: it is still sitting there,
    // not quietly sent at whichever rate the server preferred.
    await expect(page.getByRole("heading", { name: "Sent" })).toHaveCount(0);
  });

  await test.step("adopting the new rates does not send it either", async () => {
    await page.getByRole("button", { name: "Use new rates" }).click();
    await expect(page.getByRole("heading", { name: "Needs your attention" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Saved on this device" })).toBeVisible();
    // A human adopts rates; a background flush must not. If this ever
    // starts sending on its own, the silent re-pricing the server guard
    // exists to prevent has simply moved into the client.
    await expect(page.getByRole("heading", { name: "Sent" })).toHaveCount(0);
  });

  await test.step("sending it now succeeds, at the rate they saw and accepted", async () => {
    await page.getByRole("button", { name: "Send now" }).first().click();
    await expect(page.getByRole("heading", { name: "Sent" })).toBeVisible({ timeout: 30_000 });
    // 10 x $9.00, +10%, +15% = $113.85 — the NEW rate, because a person
    // looked at it and chose it.
    await expect(page.getByText("$113.85")).toBeVisible();
  });
});
