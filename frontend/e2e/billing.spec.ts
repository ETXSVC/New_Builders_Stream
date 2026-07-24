import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

test("billing module renders and tier-gates writes on the trial plan", async ({ page }) => {
  const uniqueSuffix = randomUUID().slice(0, 8);
  const email = `e2e-${uniqueSuffix}@billing.example`;
  const password = "correct-horse-battery-9";

  await test.step("register", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Billing Co ${uniqueSuffix}`);
    await page.getByLabel("Your name").fill("E2E Biller");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  await test.step("billing page renders with its tabs", async () => {
    await page.getByRole("link", { name: "Billing" }).click();
    await expect(page).toHaveURL(/\/billing/);
    for (const tab of ["Invoices", "Bills", "Expenses", "Subscription"]) {
      await expect(page.getByRole("tab", { name: tab })).toBeVisible();
    }
  });

  await test.step("subscription tab shows the trial", async () => {
    await page.getByRole("tab", { name: "Subscription" }).click();
    // exact: true — a loose "pro" match collides with the "Projects" nav
    // link under Playwright's strict mode.
    await expect(page.getByText("Plan", { exact: true })).toBeVisible();
    await expect(page.getByText("pro", { exact: true })).toBeVisible();
  });

  await test.step("creating a bill on the trial tier surfaces the upgrade notice", async () => {
    // Registration produces a trialing PRO subscription; accounting is an
    // ENTERPRISE module, so the write must come back as the styled
    // plan-upgrade callout — asserting on it proves the whole wiring
    // (form -> BFF route -> backend tier gate -> PlanUpgradeNotice)
    // end-to-end, which is the honest e2e-testable path since no API
    // exposes a tier bump on a live backend.
    await page.getByRole("tab", { name: "Bills" }).click();
    await page.getByRole("link", { name: "New bill" }).click();
    await expect(page).toHaveURL(/\/billing\/bills\/new/);
    await page.getByLabel("Vendor name").fill("E2E Lumber Supply");
    await page.getByLabel("Amount").fill("125.50");
    await page.getByRole("button", { name: "Create bill" }).click();
    await expect(page.getByText("Plan upgrade required")).toBeVisible({ timeout: 15_000 });
  });
});
