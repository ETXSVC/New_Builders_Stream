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
