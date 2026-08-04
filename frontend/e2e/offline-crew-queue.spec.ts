import { randomUUID } from "node:crypto";
import { test, expect, request as playwrightRequest, type Page, type APIRequestContext } from "@playwright/test";

const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";
const ADMIN_PASSWORD = "correct-horse-battery-9";
const CREW_PASSWORD = "anothersecret123";

/**
 * The field crew's offline write queue, end to end.
 *
 * A `field_crew` user can make exactly two writes in the whole product — a
 * task's status, and a daily log — and this drives both of them with the
 * network switched off, then proves what arrived when it came back.
 *
 * The claim that needs a browser rather than a unit test is not "the queue
 * stores things". It is that the two writes are **safe to replay**: a daily
 * log written once stays one log (the table cannot be repaired if it does
 * not — no runtime role holds UPDATE or DELETE on it), and a status change
 * made hours ago does not silently overwrite a decision taken since.
 *
 * MUST RUN AGAINST A PRODUCTION BUILD, like the estimator's spec: the cold
 * start relies on a cached document hydrating under its own CSP, and
 * `next dev`'s `'unsafe-eval'` would mask a failure.
 */

interface Crew {
  email: string;
  userId: string;
  taskId: string;
  projectId: string;
}

/** Admin registers, creates a project, a phase, and a task for the crew. */
async function seedCrewMember(page: Page, suffix: string): Promise<Crew> {
  const adminEmail = `e2e-crew-admin-${suffix}@foundation.example`;
  const crewEmail = `e2e-crew-${suffix}@foundation.example`;

  await page.goto("/register");
  await page.getByLabel("Company name").fill(`E2E Crew Co ${suffix}`);
  await page.getByLabel("Your name").fill("E2E Crew Admin");
  await page.getByLabel("Email").fill(adminEmail);
  await page.getByLabel("Password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });

  // The rest through the API: this spec is about the crew's screen, and
  // driving an admin through four forms to arrange a task would test the
  // admin's screens instead.
  const api = await playwrightRequest.newContext({ baseURL: BACKEND_URL });
  const login = await api.post("/auth/login", {
    data: { email: adminEmail, password: ADMIN_PASSWORD },
  });
  expect(login.ok()).toBeTruthy();
  const token = (await login.json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };

  const invite = await api.post("/invitations", {
    headers,
    data: { email: crewEmail, role: "field_crew" },
  });
  expect(invite.ok(), await invite.text()).toBeTruthy();
  const accept = await api.post(`/invitations/${(await invite.json()).id}/accept`, {
    data: { full_name: "E2E Crew Member", password: CREW_PASSWORD },
  });
  expect(accept.ok(), await accept.text()).toBeTruthy();

  const crewLogin = await api.post("/auth/login", {
    data: { email: crewEmail, password: CREW_PASSWORD },
  });
  expect(crewLogin.ok()).toBeTruthy();
  const crewToken = (await crewLogin.json()).access_token;
  const userId = JSON.parse(
    Buffer.from(crewToken.split(".")[1], "base64").toString("utf8")
  ).sub as string;

  const project = await api.post("/projects", {
    headers,
    data: { name: `Site ${suffix}`, site_address: "1 Main St" },
  });
  expect(project.ok(), await project.text()).toBeTruthy();
  const projectId = (await project.json()).id;

  const phase = await api.post(`/projects/${projectId}/phases`, {
    headers,
    data: { name: "Foundation", sequence: 0 },
  });
  expect(phase.ok(), await phase.text()).toBeTruthy();

  const task = await api.post(`/projects/${projectId}/tasks`, {
    headers,
    data: { name: "Pour footings", phase_id: (await phase.json()).id, assignee_id: userId },
  });
  expect(task.ok(), await task.text()).toBeTruthy();
  const taskId = (await task.json()).id;

  await api.dispose();
  return { email: crewEmail, userId, taskId, projectId };
}

async function signInAsCrew(page: Page, crew: Crew): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(crew.email);
  await page.getByLabel("Password").fill(CREW_PASSWORD);
  await page.getByRole("button", { name: "Log in" }).click();
  // field_crew lands on /my-tasks — its own role landing page.
  await expect(page).toHaveURL(/\/my-tasks/, { timeout: 15_000 });
}

async function primeForOffline(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Make available offline" }).click();
  await expect(page.getByText(/^Stored /)).toBeVisible({ timeout: 30_000 });
  await page.waitForFunction(() => !!navigator.serviceWorker.controller, null, {
    timeout: 15_000,
  });
}

async function adminApi(): Promise<APIRequestContext> {
  return playwrightRequest.newContext({ baseURL: BACKEND_URL });
}

test("field crew: both writes survive a day with no signal, and land exactly once", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const suffix = randomUUID().slice(0, 8);
  let crew: Crew;

  await test.step("seed a crew member with an assigned task, and prime", async () => {
    crew = await seedCrewMember(page, suffix);
    await signInAsCrew(page, crew);
    await expect(page.getByText("Pour footings")).toBeVisible({ timeout: 15_000 });
    await primeForOffline(page);
  });

  const cspViolations: string[] = [];
  page.on("console", (message) => {
    if (/content security policy/i.test(message.text())) cspViolations.push(message.text());
  });

  await test.step("cold-start with no network", async () => {
    await context.setOffline(true);
    await page.goto("/my-tasks");
    await expect(page.getByRole("heading", { name: "My tasks" })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("Offline — your changes are saved on this device")).toBeVisible();
    // From IndexedDB, with no token and no network.
    await expect(page.getByText("Pour footings")).toBeVisible();
  });

  await test.step("change a status and write a daily log, offline", async () => {
    await page.getByLabel("Status for Pour footings").selectOption("done");
    await expect(page.getByRole("heading", { name: "Waiting to send" })).toBeVisible();

    await page.getByLabel("Project").selectOption({ label: `Site ${suffix}` });
    await page.getByLabel("Weather").fill("Rain, 50F");
    // Typing into a controlled input is what proves the cached document
    // hydrated rather than merely rendered.
    await page.getByLabel("Notes").fill("Footings poured, crew of four.");
    await expect(page.getByLabel("Notes")).toHaveValue("Footings poured, crew of four.");
    await page.getByRole("button", { name: "Save daily log" }).click();

    await expect(page.getByText(/^Daily log — Site /)).toBeVisible();
    expect(cspViolations, "a cached document must not violate its cached policy").toEqual([]);
  });

  await test.step("connectivity returns, and both writes send themselves", async () => {
    await context.setOffline(false);
    await expect(page.getByRole("heading", { name: "Waiting to send" })).toHaveCount(0, {
      timeout: 60_000,
    });
    await expect(page.getByRole("heading", { name: "Needs your attention" })).toHaveCount(0);
  });

  await test.step("the server holds exactly one of each", async () => {
    const api = await adminApi();
    const login = await api.post("/auth/login", {
      data: { email: crew.email, password: CREW_PASSWORD },
    });
    const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };

    const tasks = await api.get("/tasks?assignee=me", { headers });
    expect(tasks.ok(), await tasks.text()).toBeTruthy();
    expect((await tasks.json()).items[0].status).toBe("done");

    const logs = await api.get(`/projects/${crew.projectId}/daily-logs`, { headers });
    expect(logs.ok(), await logs.text()).toBeTruthy();
    const items = (await logs.json()).items;
    // ONE. The queue may have sent it more than once — a lost response is
    // exactly the case the key exists for — and this table can never be
    // cleaned up, so "one" is the whole point.
    expect(items).toHaveLength(1);
    expect(items[0].notes).toBe("Footings poured, crew of four.");
    expect(items[0].client_reference).not.toBeNull();
    await api.dispose();
  });
});

test("a replayed daily log does not become a second one", async ({ page }) => {
  test.setTimeout(180_000);
  const suffix = randomUUID().slice(0, 8);

  // Driven through the API rather than the browser, because the failure it
  // guards against cannot be produced through the UI: the queue only retries
  // when it did not hear back, and a browser that hears back does not retry.
  // Sending the identical request twice is what the network does to it.
  const crew = await seedCrewMember(page, suffix);

  const api = await adminApi();
  const login = await api.post("/auth/login", {
    data: { email: crew.email, password: CREW_PASSWORD },
  });
  const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const body = {
    log_date: "2026-08-04",
    weather: "Clear",
    notes: "Same log, sent twice.",
    client_reference: randomUUID(),
  };

  const first = await api.post(`/projects/${crew.projectId}/daily-logs`, { headers, data: body });
  const second = await api.post(`/projects/${crew.projectId}/daily-logs`, { headers, data: body });
  expect(first.ok(), await first.text()).toBeTruthy();
  expect(second.ok(), await second.text()).toBeTruthy();
  expect((await second.json()).id).toBe((await first.json()).id);

  const logs = await api.get(`/projects/${crew.projectId}/daily-logs`, { headers });
  expect((await logs.json()).items).toHaveLength(1);
  await api.dispose();
});

test("a status change made offline does not overwrite a decision taken since", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const suffix = randomUUID().slice(0, 8);
  let crew: Crew;

  await test.step("seed, prime, and change the status offline", async () => {
    crew = await seedCrewMember(page, suffix);
    await signInAsCrew(page, crew);
    await expect(page.getByText("Pour footings")).toBeVisible({ timeout: 15_000 });
    await primeForOffline(page);

    await context.setOffline(true);
    await page.goto("/my-tasks");
    await expect(page.getByRole("heading", { name: "My tasks" })).toBeVisible({ timeout: 15_000 });
    await page.getByLabel("Status for Pour footings").selectOption("done");
    await expect(page.getByRole("heading", { name: "Waiting to send" })).toBeVisible();
  });

  await test.step("meanwhile, a project manager moves the same task", async () => {
    // The real scenario: the crew member is out of signal, the rest of the
    // world is not.
    const api = await adminApi();
    const login = await api.post("/auth/login", {
      data: { email: `e2e-crew-admin-${suffix}@foundation.example`, password: ADMIN_PASSWORD },
    });
    const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
    const moved = await api.patch(`/tasks/${crew.taskId}`, {
      headers,
      data: { status: "in_progress" },
    });
    expect(moved.ok(), await moved.text()).toBeTruthy();
    await api.dispose();
  });

  await test.step("the queued change is refused, not applied silently", async () => {
    await context.setOffline(false);
    await expect(page.getByRole("heading", { name: "Needs your attention" })).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByText(/now 'in_progress'/)).toBeVisible();

    const api = await adminApi();
    const login = await api.post("/auth/login", {
      data: { email: crew.email, password: CREW_PASSWORD },
    });
    const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
    const tasks = await api.get("/tasks?assignee=me", { headers });
    // The project manager's decision stands until a person says otherwise.
    expect((await tasks.json()).items[0].status).toBe("in_progress");
    await api.dispose();
  });

  await test.step("applying it anyway is a decision the crew member makes", async () => {
    await page.getByRole("button", { name: /Apply Done anyway/ }).click();
    await expect(page.getByRole("heading", { name: "Needs your attention" })).toHaveCount(0, {
      timeout: 60_000,
    });

    const api = await adminApi();
    const login = await api.post("/auth/login", {
      data: { email: crew.email, password: CREW_PASSWORD },
    });
    const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
    const tasks = await api.get("/tasks?assignee=me", { headers });
    expect((await tasks.json()).items[0].status).toBe("done");
    await api.dispose();
  });
});
