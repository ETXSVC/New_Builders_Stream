import { randomUUID } from "node:crypto";
import { test, expect, request as playwrightRequest } from "@playwright/test";

const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";

/**
 * The rate-conflict path, end to end.
 *
 * The server-side guard has unit coverage; this is the half that did not:
 * that a refused save is *recoverable* from the screen. The failure it
 * guards against is a UI one — an estimator told "the rate changed" with no
 * way to act on it, whose only recourse is deleting and re-adding every
 * line to pick the new rates up by hand.
 *
 * The catalog is edited **out of band**, through the API rather than the
 * UI, because that is the real scenario: a colleague repriced the catalog
 * while this estimator was mid-build. Doing it through the browser would
 * mean navigating away from the builder, which discards the draft and
 * destroys the very state under test.
 */
test("estimate rate conflict: refused, explained, and recoverable", async ({ page }) => {
  test.setTimeout(180_000);

  const suffix = randomUUID().slice(0, 8);
  const adminEmail = `e2e-rate-${suffix}@foundation.example`;
  const password = "correct-horse-battery-9";

  await test.step("register and seed a catalog item and markup profile", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Rate Co ${suffix}`);
    await page.getByLabel("Your name").fill("E2E Rate Tester");
    await page.getByLabel("Email").fill(adminEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });

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
  });

  await test.step("create a project and an estimate", async () => {
    await page.getByRole("link", { name: "Projects", exact: true }).click();
    await page.getByRole("link", { name: "New project" }).click();
    await page.getByLabel("Project name").fill(`Deck ${suffix}`);
    await page.getByLabel("Site address").fill("1 Main St");
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(page.getByRole("heading", { name: `Deck ${suffix}` })).toBeVisible({
      timeout: 15_000,
    });

    await page.getByRole("tab", { name: "Estimates" }).click();
    await page.getByRole("link", { name: "New estimate" }).click();
    await page.getByLabel("Markup profile").selectOption({ label: "Standard" });
    await page.getByRole("button", { name: "Create estimate" }).click();
    await expect(page).toHaveURL(/\/estimates\/[0-9a-f-]+$/, { timeout: 15_000 });
  });

  await test.step("build a line at the rate the estimator can see", async () => {
    await page.getByRole("button", { name: "+" }).first().click();
    await page.getByLabel(/Quantity for/).fill("10");
    // 10 x $4.00, in both the line total and the subtotal beneath it —
    // `LineRows` uses the default whole-dollar formatting, so "$40", not
    // "$40.00". Asserting the count rather than the first match makes this
    // a statement about the whole panel.
    await expect(page.getByText("$40", { exact: true })).toHaveCount(2);
    // Nothing calculated yet, so no PDF panel — this is the saved/unsaved
    // signal used throughout: `estimate.total !== null` gates it.
    await expect(page.getByRole("button", { name: "Generate PDF" })).toHaveCount(0);
  });

  await test.step("a colleague reprices the catalog, out of band", async () => {
    const api = await playwrightRequest.newContext({ baseURL: BACKEND_URL });
    const login = await api.post("/auth/login", { data: { email: adminEmail, password } });
    expect(login.ok()).toBeTruthy();
    const token = (await login.json()).access_token;

    const items = await api.get("/catalogs/items", {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(items.ok()).toBeTruthy();
    const lumber = (await items.json()).items.find(
      (i: { name: string }) => i.name === "Lumber"
    );
    expect(lumber, "seeded catalog item should be listed").toBeTruthy();

    const patched = await api.patch(`/catalogs/items/${lumber.id}`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { unit_rate: "9.00" },
    });
    expect(patched.ok()).toBeTruthy();
    await api.dispose();
  });

  await test.step("the save is refused, and says what moved", async () => {
    await page.getByRole("button", { name: "Save & calculate" }).click();

    // Not `getByRole("alert")`: Next's own route announcer
    // (`#__next-route-announcer__`) is also role=alert, so that resolves to
    // two elements and fails strict mode.
    await expect(page.getByText(/rate\(s\) changed/i)).toBeVisible({ timeout: 15_000 });
    // Both rates, precisely — whole-dollar rounding would render a 4.00 ->
    // 4.05 conflict as "$4 -> $4", which is worse than saying nothing.
    //
    // Scoped to the conflict notice's own row rather than searched for across
    // the page. These used to be bare `getByText("$4.00")` calls, which
    // worked only because that string happened to appear nowhere else — and
    // stopped working the moment the line rows above grew a unit-rate column
    // showing the same figure. Asserting inside the row that claims the
    // conflict is what the step actually means, and it no longer depends on
    // the rest of the screen staying quiet.
    const conflictRow = page.getByRole("listitem").filter({ hasText: "Lumber" });
    await expect(conflictRow.getByText("$4.00", { exact: true })).toBeVisible();
    await expect(conflictRow.getByText("$9.00", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Use new rates" })).toBeVisible();

    // The point of the guard: nothing was written.
    await expect(page.getByRole("button", { name: "Generate PDF" })).toHaveCount(0);
  });

  await test.step("adopting the new rates updates the draft but does NOT save", async () => {
    await page.getByRole("button", { name: "Use new rates" }).click();

    // 10 x $9.00 — the estimator sees the new total before committing to it.
    await expect(page.getByText("$90", { exact: true })).toHaveCount(2);
    await expect(page.getByRole("button", { name: "Use new rates" })).toHaveCount(0);
    // Still unsaved. Auto-saving here would be the silent re-pricing the
    // whole feature exists to prevent, moved into the client.
    await expect(page.getByRole("button", { name: "Generate PDF" })).toHaveCount(0);
  });

  await test.step("saving now succeeds", async () => {
    await page.getByRole("button", { name: "Save & calculate" }).click();
    await expect(page.getByRole("button", { name: "Generate PDF" })).toBeVisible({
      timeout: 15_000,
    });
  });
});
