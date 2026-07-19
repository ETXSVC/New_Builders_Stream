import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

test("lead to won to drafted project through documents and daily logs", async ({ page }) => {
  const suffix = randomUUID().slice(0, 8);
  const email = `e2e-crm-${suffix}@foundation.example`;
  const password = "correct-horse-battery-9";

  await test.step("register and land on dashboard", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E CRM Co ${suffix}`);
    await page.getByLabel("Your name").fill("E2E CRM Tester");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
    await expect(page.getByText("Open leads")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("create a lead and log a communication", async () => {
    await page.getByRole("link", { name: "Leads" }).click();
    await page.getByRole("link", { name: "New lead" }).click();
    await page.getByLabel("Contact name").fill("Ada Contact");
    await page.getByLabel("Project name").fill(`Kitchen ${suffix}`);
    await page.getByLabel("Email").fill(`ada-${suffix}@client.example`);
    await page.getByLabel("Project type").fill("Remodel");
    await page.getByRole("button", { name: "Create lead" }).click();
    await expect(page.getByRole("heading", { name: "Ada Contact" })).toBeVisible({ timeout: 15_000 });

    await page.getByLabel("Communication summary").fill("Discussed budget range");
    await page.getByRole("button", { name: "Add", exact: true }).click();
    await expect(page.getByText("Discussed budget range")).toBeVisible();
  });

  await test.step("walk the lead to won", async () => {
    for (const label of ["Mark contacted", "Mark estimating", "Mark qualified", "Mark won"]) {
      await page.getByRole("button", { name: label }).click();
      await expect(page.getByRole("button", { name: label })).toBeHidden();
    }
    await expect(page.getByText("a draft project was created automatically")).toBeVisible();
  });

  await test.step("open the drafted project and fill its site address", async () => {
    await page.getByRole("link", { name: "Open projects" }).click();
    await page.getByRole("link", { name: `Kitchen ${suffix}` }).click();
    await expect(page.getByRole("heading", { name: `Kitchen ${suffix}` })).toBeVisible({ timeout: 15_000 });
    await page.getByLabel("Site address").fill("412 Maple St");
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText("Saved.")).toBeVisible();
  });

  await test.step("advance the project to active", async () => {
    await page.getByRole("button", { name: "Move to pre-construction" }).click();
    await expect(page.getByRole("button", { name: "Move to active" })).toBeVisible();
    await page.getByRole("button", { name: "Move to active" }).click();
    await expect(page.getByRole("button", { name: "Move to completed" })).toBeVisible();
  });

  await test.step("add a phase and a task, mark it done", async () => {
    await page.getByRole("tab", { name: "Phases & tasks" }).click();
    await page.getByLabel("New phase name").fill("Framing");
    await page.getByRole("button", { name: "Add phase" }).click();
    await expect(page.getByRole("button", { name: /Framing/ })).toBeVisible();

    await page.getByLabel("New task name").fill("Frame walls");
    await page.getByRole("button", { name: "Add task" }).click();
    await expect(page.getByText("Frame walls")).toBeVisible();

    await page.getByLabel("Status for Frame walls").selectOption("done");
    await expect(page.getByText("1 done")).toBeVisible();
  });

  await test.step("upload a document and download it back", async () => {
    await page.getByRole("tab", { name: "Documents" }).click();
    await page.getByLabel("Choose file").setInputFiles({
      name: "site-plan.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("blueprint bytes"),
    });
    await page.getByRole("button", { name: "Upload" }).click();
    await expect(page.getByText("site-plan.txt")).toBeVisible();

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("site-plan.txt");
    // Spec Decision 8: assert the CONTENT round-trips, not just the name.
    const downloadPath = await download.path();
    expect(readFileSync(downloadPath, "utf-8")).toBe("blueprint bytes");
  });

  await test.step("add a daily log", async () => {
    await page.getByRole("tab", { name: "Daily logs" }).click();
    await page.getByLabel("Notes").fill("Poured foundation, clear skies.");
    await page.getByRole("button", { name: "Add log entry" }).click();
    await expect(page.getByText("Poured foundation, clear skies.")).toBeVisible();
  });

  await test.step("dashboard reflects the data", async () => {
    await page.goto("/dashboard");
    await expect(page.getByText("Active projects")).toBeVisible({ timeout: 15_000 });
    // The company has exactly one active project (created in this test).
    const activeCard = page.locator("div").filter({ hasText: /^Active projects/ }).last();
    await expect(activeCard).toContainText("1");
  });
});
