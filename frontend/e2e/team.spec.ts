import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

/**
 * The team directory, end to end.
 *
 * `backend/tests/test_team.py` already covers the API's own rules — the
 * two-companies-one-person isolation, the case-insensitive profession
 * clash, the 409 on a concurrent edit, ON DELETE SET NULL, the non-image
 * 422. This spec deliberately does not re-litigate any of that. It covers
 * the half a backend test cannot see:
 *
 *   * **The photo actually renders.** It is the one piece with no backend
 *     equivalent and the most fragile: the bytes are behind a role check,
 *     the product session keeps its token in memory rather than a cookie,
 *     so `<img src>` cannot work and the component fetches and hands the
 *     element an object URL. If that path breaks, every other test in this
 *     repo still passes.
 *   * **Clearing a field really clears it**, through the form's
 *     empty-string-means-null translation rather than through a JSON body
 *     a test wrote by hand.
 *   * **The role split as a user meets it** — a project manager reading
 *     the same record with no controls, rather than a 403.
 *   * **The signed-out redirect**, which is `middleware.ts`'s matcher and
 *     nothing else. `/team` was missing from it once already.
 *
 * The invitee is set up through the API on purpose: `invitations.spec.ts`
 * owns the accept PAGE, and re-driving it here would buy nothing and cost
 * a page load.
 */

const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";

// A real 1x1 PNG. Real, rather than arbitrary bytes, because the assertion
// is that the browser DECODES what came back through the blob URL — bytes
// that fail to decode would leave a broken <img> that still matches a
// locator.
const ONE_PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64"
);

test("an admin fills in a teammate's record, and a project manager can only read it", async ({
  page,
  request,
}) => {
  // 3x the config's 60s, for the reason crm-pm.spec.ts spells out: this arc
  // hits five Route Handlers and two pages for the first time on a cold
  // `next dev` server. The per-assertion timeouts below still bound each
  // individual transition.
  test.setTimeout(180_000);

  const suffix = randomUUID().slice(0, 8);
  // ".example", not ".test" — the backend's EmailStr rejects special-use
  // domains (see foundation.spec.ts).
  const adminEmail = `e2e-${suffix}@team-admin.example`;
  const pmEmail = `e2e-${suffix}@team-pm.example`;
  const password = "correct-horse-battery-9";

  await test.step("register the admin", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Team Co ${suffix}`);
    await page.getByLabel("Your name").fill("E2E Team Admin");
    await page.getByLabel("Email").fill(adminEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  await test.step("add a project manager to the company through the API", async () => {
    const login = await request.post(`${BACKEND_URL}/auth/login`, {
      data: { email: adminEmail, password },
    });
    expect(login.ok()).toBeTruthy();
    const { access_token } = await login.json();

    const invite = await request.post(`${BACKEND_URL}/invitations`, {
      headers: { Authorization: `Bearer ${access_token}` },
      data: { email: pmEmail, role: "project_manager" },
    });
    expect(invite.status()).toBe(201);

    const accepted = await request.post(
      `${BACKEND_URL}/invitations/${(await invite.json()).id}/accept`,
      { data: { full_name: "E2E Team PM", password } }
    );
    expect(accepted.ok()).toBeTruthy();
  });

  await test.step("the directory lists everyone, profile or not", async () => {
    await page.getByRole("link", { name: "Team", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Team" })).toBeVisible({ timeout: 15_000 });

    // Both people, under the names on their ACCOUNTS — neither has a
    // profile row yet, and a member who has just joined must still appear.
    await expect(page.getByRole("link", { name: /E2E Team Admin/ })).toBeVisible();
    await expect(page.getByRole("link", { name: /E2E Team PM/ })).toBeVisible();
  });

  await test.step("a duplicate profession is refused, case and all", async () => {
    await page.getByLabel("Add a profession").fill("Electrician");
    await page.getByRole("button", { name: "Add", exact: true }).click();
    // The chip's own remove control, rather than getByText: "Electrician"
    // appears in the chip and later in a table row and a <select>, and a
    // text locator would start matching several of them.
    await expect(page.getByRole("button", { name: "Remove Electrician" })).toBeVisible({
      timeout: 15_000,
    });

    await page.getByLabel("Add a profession").fill("electrician");
    await page.getByRole("button", { name: "Add", exact: true }).click();
    // The backend's unique index is on lower(name), and the panel surfaces
    // its 409 rather than silently doing nothing.
    await expect(page.getByText(/already exists/i)).toBeVisible({ timeout: 15_000 });
  });

  await test.step("fill in the project manager's record", async () => {
    await page.getByRole("link", { name: /E2E Team PM/ }).click();
    await expect(page.getByRole("heading", { name: "E2E Team PM" })).toBeVisible({
      timeout: 15_000,
    });

    await page.getByLabel("First name").fill("Dale");
    await page.getByLabel("Last name").fill("Rivera");
    await page.getByLabel("City").fill("Tyler");
    await page.getByLabel("Profession").selectOption({ label: "Electrician" });

    await page.getByRole("button", { name: "Add a phone" }).click();
    await page.getByLabel("Label").fill("mobile");
    // Stored as typed: the extension is the part a normalising parser would
    // throw away, and the backend deliberately does not have one.
    await page.getByLabel("Number").fill("903-555-0199 x12");

    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("Saved.")).toBeVisible({ timeout: 15_000 });

    // The heading now uses the company's record of them rather than the
    // name on their account.
    await expect(page.getByRole("heading", { name: "Dale Rivera" })).toBeVisible();
  });

  await test.step("the photo survives the round trip and renders", async () => {
    await page.getByLabel("Photo file").setInputFiles({
      name: "face.png",
      mimeType: "image/png",
      buffer: ONE_PIXEL_PNG,
    });
    await page.getByRole("button", { name: "Upload photo" }).click();

    // The bytes come back through an authenticated fetch and an object URL,
    // so this only passes if the BFF, the role check and the blob plumbing
    // all work — the initials fallback is aria-hidden and has no img role.
    const photo = page.getByRole("img", { name: "Dale Rivera" });
    await expect(photo).toBeVisible({ timeout: 15_000 });
    await expect(photo).toHaveJSProperty("naturalWidth", 1);
  });

  await test.step("an emptied field is cleared, not left alone", async () => {
    await page.getByLabel("City").fill("");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("Saved.")).toBeVisible({ timeout: 15_000 });

    // Re-read from the server rather than trusting the form: "" has to
    // reach the backend as null, or the field could never be emptied.
    await page.reload();
    await expect(page.getByLabel("City")).toHaveValue("", { timeout: 15_000 });
    await expect(page.getByLabel("First name")).toHaveValue("Dale");
  });

  await test.step("the list shows what was filed", async () => {
    await page.getByRole("link", { name: "← Team" }).click();
    await expect(page.getByRole("link", { name: /Dale Rivera/ })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("link", { name: /Electrician/ })).toBeVisible();
  });

  await test.step("retiring a profession releases whoever held it", async () => {
    await page.getByRole("button", { name: "Remove Electrician" }).click();
    await page.getByRole("button", { name: "Remove", exact: true }).click();
    // ON DELETE SET NULL: Dale keeps their record and loses the trade.
    await expect(page.getByRole("link", { name: /Electrician/ })).toBeHidden({ timeout: 15_000 });
    await expect(page.getByRole("link", { name: /Dale Rivera/ })).toBeVisible();
  });

  await test.step("a project manager reads the record but gets no controls", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/);
    await page.getByLabel("Email").fill(pmEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });

    await page.getByRole("link", { name: "Team", exact: true }).click();
    await page.getByRole("link", { name: /Dale Rivera/ }).click();
    await expect(page.getByRole("heading", { name: "Dale Rivera" })).toBeVisible({
      timeout: 15_000,
    });

    // Reads are admin + project_manager; writes are admin only. The page is
    // whole and inert rather than a 403 they cannot act on.
    await expect(page.getByText("Only an admin can change")).toBeVisible();
    await expect(page.getByRole("button", { name: "Save" })).toBeHidden();
    await expect(page.getByLabel("First name")).toBeDisabled();
  });

  await test.step("signed out, /team redirects before it renders", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/);

    // Asserted as a REDIRECT rather than as a final URL, because the final
    // URL is the same either way: with /team missing from middleware.ts's
    // matcher the page returns 200, renders, fails to refresh a session and
    // only then bounces client-side — which `toHaveURL(/\/login/)` cannot
    // tell apart from the edge redirect. `redirectedFrom()` can: it is
    // populated only when the server answered the /team request with a 307.
    const response = await page.goto("/team");
    expect(response?.request().redirectedFrom()?.url()).toContain("/team");
    await expect(page).toHaveURL(/\/login/);
  });
});
