import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

test("compliance module: create a subcontractor, see the registry and dashboard", async ({ page }) => {
  const uniqueSuffix = randomUUID().slice(0, 8);
  const email = `e2e-${uniqueSuffix}@compliance.example`;
  const password = "correct-horse-battery-9";
  const subcontractorName = `E2E Electric ${uniqueSuffix}`;

  await test.step("register", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Compliance Co ${uniqueSuffix}`);
    await page.getByLabel("Your name").fill("E2E Compliance Admin");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  await test.step("create a subcontractor", async () => {
    // The trial subscription is PRO, and compliance is a PRO module — so
    // unlike billing.spec.ts's enterprise-gated write, this whole flow
    // works on a fresh registration.
    await page.getByRole("link", { name: "Compliance" }).click();
    await expect(page).toHaveURL(/\/compliance/);
    await page.getByRole("link", { name: "Manage subcontractors" }).click();
    await expect(page).toHaveURL(/\/subcontractors/);
    await page.getByRole("link", { name: "New subcontractor" }).click();
    await page.getByLabel("Name").fill(subcontractorName);
    await page.getByLabel("Trade (optional)").fill("Electrical");
    await page.getByRole("button", { name: "Create subcontractor" }).click();
    // Lands on the new subcontractor's detail page.
    await expect(page.getByRole("heading", { name: subcontractorName })).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText("No compliance documents on file.")).toBeVisible();
  });

  await test.step("registry lists it", async () => {
    await page.goto("/subcontractors");
    await expect(page.getByRole("link", { name: new RegExp(subcontractorName) })).toBeVisible();
  });

  await test.step("compliance dashboard renders (empty — no documents yet)", async () => {
    await page.goto("/compliance");
    await expect(page.getByRole("heading", { name: "Compliance" })).toBeVisible();
    await expect(page.getByText("No compliance documents on file yet", { exact: false })).toBeVisible();
  });
});

test("a failed expiry-notifications load is shown, not silently rendered as empty", async ({
  page,
}) => {
  // The regression this pins (review finding M5): the notifications fetch
  // used to be `if (response.ok) { setNotifications(...) }` with no else,
  // so a 500 left the section rendering nothing at all — and on a
  // compliance page "no documents are expiring" and "we could not find
  // out whether any are" are very different statements that looked
  // identical.
  //
  // Driven with request interception rather than by breaking the backend:
  // this is a client-side error path, and forcing it at the network layer
  // is the only way to exercise it without a fixture whose whole purpose
  // is to fail.
  const uniqueSuffix = randomUUID().slice(0, 8);
  const email = `e2e-${uniqueSuffix}@compliance-err.example`;
  const password = "correct-horse-battery-9";

  await page.goto("/register");
  await page.getByLabel("Company name").fill(`E2E Compliance Err Co ${uniqueSuffix}`);
  await page.getByLabel("Your name").fill("E2E Compliance Admin");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });

  await page.route("**/api/compliance/notifications**", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Expiry notifications are unavailable." }),
    }),
  );

  await page.goto("/compliance");

  // The failure is stated, it names what failed, and it carries role=alert
  // so a screen reader announces it rather than leaving the section
  // silently empty in a different way.
  //
  // Filtered rather than a bare getByRole("alert"): Next.js renders its own
  // always-present route announcer with that role, so the unfiltered
  // locator is ambiguous and resolves to the wrong (empty) element.
  await expect(
    page.getByRole("alert").filter({ hasText: "Expiry notifications are unavailable." }),
  ).toBeVisible({ timeout: 15_000 });
  // ...with a way out that doesn't require knowing to reload the page.
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();

  // And the dashboard half, which loaded fine, is still rendered — the two
  // fetches are independent and one failing must not blank the other.
  await expect(page.getByRole("heading", { name: "Compliance" })).toBeVisible();
  await expect(
    page.getByText("No compliance documents on file yet", { exact: false }),
  ).toBeVisible();
});
