import { randomUUID } from "node:crypto";
import { test, expect, request as playwrightRequest } from "@playwright/test";

const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";

test("estimation and e-signature: catalog, builder, PDF, client sign-off, change order", async ({ page }) => {
  test.setTimeout(240_000);

  const suffix = randomUUID().slice(0, 8);
  const adminEmail = `e2e-est-${suffix}@foundation.example`;
  const clientEmail = `e2e-est-client-${suffix}@foundation.example`;
  const password = "correct-horse-battery-9";

  let estimateId = "";
  let adminAccessToken = "";

  await test.step("register admin and land on dashboard", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Estimation Co ${suffix}`);
    await page.getByLabel("Your name").fill("E2E Estimation Tester");
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
    await page.getByLabel("Project name").fill(`Deck ${suffix}`);
    await page.getByLabel("Site address").fill("1 Main St");
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(page.getByRole("heading", { name: `Deck ${suffix}` })).toBeVisible({ timeout: 15_000 });

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

  await test.step("export and download the PDF", async () => {
    await page.getByRole("button", { name: "Generate PDF" }).click();
    await expect(page.getByRole("button", { name: "Download" })).toBeVisible({ timeout: 30_000 });
  });

  await test.step("send for signature", async () => {
    await page.getByRole("button", { name: "Send for signature" }).click();
    await expect(page.getByText("Waiting for the client's signature.")).toBeVisible({ timeout: 15_000 });
    estimateId = page.url().split("/estimates/")[1];
  });

  await test.step("seed a client user via the backend invitation API and sign as them", async () => {
    // Extract the admin's access token and company id from the browser's
    // in-memory AuthContext state via localStorage/sessionStorage is NOT
    // possible (the access token lives only in React state per the BFF
    // session design, never in web storage) — instead, log the admin in
    // again directly against the backend to obtain a fresh token for this
    // API-only client, independent of the browser session.
    const apiContext = await playwrightRequest.newContext({ baseURL: BACKEND_URL });

    const loginResponse = await apiContext.post("/auth/login", {
      data: { email: adminEmail, password },
    });
    expect(loginResponse.ok()).toBeTruthy();
    const loginBody = await loginResponse.json();
    adminAccessToken = loginBody.access_token;

    const invitationResponse = await apiContext.post("/invitations", {
      headers: { Authorization: `Bearer ${adminAccessToken}` },
      data: { email: clientEmail, role: "client" },
    });
    expect(invitationResponse.ok()).toBeTruthy();
    const invitation = await invitationResponse.json();

    const acceptResponse = await apiContext.post(`/invitations/${invitation.id}/accept`, {
      data: { password, full_name: "E2E Client" },
    });
    expect(acceptResponse.ok()).toBeTruthy();

    await apiContext.dispose();

    // Now sign in as the client THROUGH THE BROWSER (a real UI session,
    // not the API context above) — the whole point of this arc is
    // verifying the in-app typed-signature UI actually works.
    await page.getByRole("button", { name: "Log out" }).click();
    // 15s, not the 5s default: this is the first hit of /login within this
    // spec's own worker (unlike foundation.spec.ts, which owns login/logout
    // coverage) — same cold `next dev` compile cost documented in
    // playwright.config.ts and crm-pm.spec.ts for first-hit transitions.
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
    await page.getByLabel("Email").fill(clientEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page).toHaveURL(/\/(dashboard|projects)/, { timeout: 15_000 });

    await page.goto(`/estimates/${estimateId}`);
    await expect(page.getByLabel("Full name")).toBeVisible({ timeout: 15_000 });
    await page.getByLabel("Full name").fill("E2E Client");
    await page.getByLabel("Email", { exact: true }).fill(clientEmail);
    await page.getByRole("button", { name: "Approve & sign" }).click();
    await expect(page.getByText("Signed")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("admin duplicates the approved estimate", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
    await page.getByLabel("Email").fill(adminEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });

    await page.goto(`/estimates/${estimateId}`);
    await page.getByRole("button", { name: "Duplicate as new draft" }).click();
    await expect(page).toHaveURL(/\/estimates\/[0-9a-f-]+$/, { timeout: 15_000 });
    // The duplicated estimate is a fresh draft, which renders the
    // two-panel builder (not the read-only "Qty N @ rate" list that only
    // sent/approved/rejected estimates show) — so the copied line item's
    // quantity surfaces as an editable input's value, not as static text.
    await expect(page.getByLabel(/Quantity for/)).toHaveValue("10.00", { timeout: 15_000 });
  });

  await test.step("change order blocks completion until approved", async () => {
    await page.getByRole("link", { name: "Projects", exact: true }).click();
    await page.getByRole("link", { name: `Deck ${suffix}` }).click();
    await page.getByRole("button", { name: "Move to pre-construction" }).click();
    await expect(page.getByRole("button", { name: "Move to active" })).toBeVisible({ timeout: 15_000 });
    await page.getByRole("button", { name: "Move to active" }).click();
    await expect(page.getByRole("button", { name: "Move to completed" })).toBeVisible({ timeout: 15_000 });

    await page.getByRole("tab", { name: "Change orders" }).click();
    await page.getByLabel("Description").fill("Add railing");
    await page.getByLabel("Cost delta").fill("250");
    await page.getByRole("button", { name: "Add change order" }).click();
    // Anchored to the rendered LIST ITEM, not bare getByText: React keeps a
    // controlled <textarea>'s defaultValue — which IS its text content — in
    // sync on every render, so getByText("Add railing") matches the form's
    // own textarea the instant it's typed, long before the create request
    // returns. That let this step race ahead and click "Move to completed"
    // while the change-order POST was still in flight; under CI load the
    // status PATCH could win, see zero pending change orders, and complete
    // the project — the exact flake e2e-ci's first run caught. The list
    // item only renders after the post-create refetch returns the
    // committed row, so waiting on it fully orders the click after the
    // change order exists.
    await expect(
      page.getByRole("listitem").filter({ hasText: "Add railing" })
    ).toBeVisible({ timeout: 15_000 });

    await page.getByRole("button", { name: "Move to completed" }).click();
    await expect(page.getByText(/pending approval/i)).toBeVisible({ timeout: 15_000 });
  });
});
