import { randomUUID } from "node:crypto";
import { test, expect, request as playwrightRequest } from "@playwright/test";

const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";

test("materials: BOM auto-generation, vendor assignment, receiving", async ({ page }) => {
  test.setTimeout(240_000);

  const suffix = randomUUID().slice(0, 8);
  const adminEmail = `e2e-bom-${suffix}@foundation.example`;
  const clientEmail = `e2e-bom-client-${suffix}@foundation.example`;
  const password = "correct-horse-battery-9";

  let estimateId = "";
  let projectId = "";

  await test.step("register admin and land on dashboard", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Materials Co ${suffix}`);
    await page.getByLabel("Your name").fill("E2E Materials Tester");
    await page.getByLabel("Email").fill(adminEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  await test.step("create a catalog item and a markup profile", async () => {
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

  await test.step("create a project and an estimate against it", async () => {
    await page.getByRole("link", { name: "Projects", exact: true }).click();
    await page.getByRole("link", { name: "New project" }).click();
    await page.getByLabel("Project name").fill(`Materials Deck ${suffix}`);
    await page.getByLabel("Site address").fill("1 Main St");
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(page.getByRole("heading", { name: `Materials Deck ${suffix}` })).toBeVisible({ timeout: 15_000 });
    projectId = page.url().split("/projects/")[1];

    await page.getByRole("tab", { name: "Estimates" }).click();
    await page.getByRole("link", { name: "New estimate" }).click();
    await page.getByLabel("Markup profile").selectOption({ label: "Standard" });
    await page.getByRole("button", { name: "Create estimate" }).click();
    await expect(page).toHaveURL(/\/estimates\/[0-9a-f-]+$/, { timeout: 15_000 });
  });

  await test.step("build the estimate and calculate", async () => {
    await page.getByRole("button", { name: "+" }).first().click();
    await page.getByLabel(/Quantity for/).fill("10");
    await page.getByRole("button", { name: "Save & calculate" }).click();
    await expect(page.getByText("Subtotal (before markup)")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("send for signature", async () => {
    await page.getByRole("button", { name: "Send for signature" }).click();
    await expect(page.getByText("Waiting for the client's signature.")).toBeVisible({ timeout: 15_000 });
    estimateId = page.url().split("/estimates/")[1];
  });

  await test.step("seed a client user via the backend invitation API, sign as them to trigger BOM auto-generation", async () => {
    // Same rationale as estimation.spec.ts: the admin's access token lives
    // only in React state (never web storage) per the BFF session design,
    // so a fresh API-only login is used here instead of reading it out of
    // the browser session.
    const apiContext = await playwrightRequest.newContext({ baseURL: BACKEND_URL });

    const loginResponse = await apiContext.post("/auth/login", {
      data: { email: adminEmail, password },
    });
    expect(loginResponse.ok()).toBeTruthy();
    const loginBody = await loginResponse.json();
    const adminAccessToken = loginBody.access_token;

    const invitationResponse = await apiContext.post("/invitations", {
      headers: { Authorization: `Bearer ${adminAccessToken}` },
      data: { email: clientEmail, role: "client" },
    });
    expect(invitationResponse.ok()).toBeTruthy();
    const invitation = await invitationResponse.json();

    const acceptResponse = await apiContext.post(`/invitations/${invitation.id}/accept`, {
      data: { password, full_name: "E2E BOM Client" },
    });
    expect(acceptResponse.ok()).toBeTruthy();

    await apiContext.dispose();

    // Sign in as the client THROUGH THE BROWSER — approval must happen via
    // the real typed-signature UI so it fires the same ESTIMATE_APPROVED
    // event the BOM auto-generation handler (backend Task 8) subscribes to.
    await page.getByRole("button", { name: "Log out" }).click();
    // 15s, not the 5s default: first hit of /login within this spec's own
    // worker pays the cold `next dev` compile cost (same reasoning as
    // estimation.spec.ts and crm-pm.spec.ts).
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
    await page.getByLabel("Email").fill(clientEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page).toHaveURL(/\/(dashboard|projects)/, { timeout: 15_000 });

    await page.goto(`/estimates/${estimateId}`);
    await expect(page.getByLabel("Full name")).toBeVisible({ timeout: 15_000 });
    await page.getByLabel("Full name").fill("E2E BOM Client");
    await page.getByLabel("Email", { exact: true }).fill(clientEmail);
    await page.getByRole("button", { name: "Approve & sign" }).click();
    await expect(page.getByText("Signed")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("log back in as admin", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
    await page.getByLabel("Email").fill(adminEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  await test.step("the auto-generated BOM line appears on the project's Materials tab", async () => {
    await page.goto(`/projects/${projectId}`);
    await page.getByRole("tab", { name: "Materials" }).click();
    await expect(page.getByText("Lumber")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("Needed")).toBeVisible();
  });

  await test.step("create a vendor via the Catalog page's Vendors tab", async () => {
    await page.goto("/catalog");
    await page.getByRole("tab", { name: "Vendors" }).click();
    await page.getByLabel("Name").fill("ABC Lumber Supply");
    await page.getByRole("button", { name: "Add vendor" }).click();
    await expect(page.getByText("ABC Lumber Supply")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("mark the material ordered with that vendor", async () => {
    await page.goto(`/projects/${projectId}`);
    await page.getByRole("tab", { name: "Materials" }).click();
    // Exact match: by this point the "Mark ordered…" combobox's options
    // include the just-created "ABC Lumber Supply" vendor, whose label
    // contains "Lumber" as a substring — a non-exact getByText("Lumber")
    // resolves to both the BomLine's description span AND that <option>,
    // a strict-mode violation.
    await expect(page.getByText("Lumber", { exact: true })).toBeVisible({ timeout: 15_000 });
    await page.getByRole("combobox").selectOption({ label: "ABC Lumber Supply" });
    await expect(page.getByText("Ordered")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("record a partial receipt", async () => {
    // The shared setup's estimate line used quantity 10 (see "build the
    // estimate and calculate" above), so a receipt of 4 leaves the line
    // short of its total and should read "partially_received".
    await page.getByPlaceholder("Qty received").fill("4");
    await page.getByRole("button", { name: "Record receipt" }).click();
    await expect(page.getByText("Partially received")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("record the remainder", async () => {
    // 4 + 6 = 10, matching the line's full quantity, which flips status to
    // "received". Matched with an exact regex because "Partially received"
    // also contains the substring "received".
    await page.getByPlaceholder("Qty received").fill("6");
    await page.getByRole("button", { name: "Record receipt" }).click();
    await expect(page.getByText(/^Received$/)).toBeVisible({ timeout: 15_000 });
  });

  await test.step("the same line surfaces correctly on the global Materials page", async () => {
    await page.goto("/materials");
    await expect(page.getByText("Lumber")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/^Received$/)).toBeVisible();
  });
});
