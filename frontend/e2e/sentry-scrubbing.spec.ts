import { test, expect } from "@playwright/test";

import { scrubEvent, scrubUrl } from "../sentry.shared";

/**
 * The Sentry scrubber, tested here because Playwright is the only test
 * runner this frontend has. These are pure functions — no browser is
 * driven — but the alternative was leaving the code that decides what
 * leaves the building untested.
 *
 * This caught a real defect on its first run. The invitation-accept page
 * reads its invitation from `?id=`, and the sensitive-key list had been
 * written from memory as `token`/`invitation`. The credential survived
 * scrubbing. Asserting on the REAL url shapes is what found it.
 */
test.describe("sentry event scrubbing", () => {
  test("redacts the invitation link, which is itself the credential", () => {
    expect(scrubUrl("/accept-invitation?id=REAL-INVITE")).not.toContain("REAL-INVITE");
  });

  test("redacts the OAuth callback's code and state", () => {
    const scrubbed = scrubUrl("/integrations/callback?code=SECRET&state=SIG");
    expect(scrubbed).not.toContain("SECRET");
    expect(scrubbed).not.toContain("SIG");
  });

  test("leaves ordinary urls alone", () => {
    // A scrubber that eats everything makes events useless and gets
    // switched off, so over-redaction is a real failure mode too.
    expect(scrubUrl("/projects/1111-2222?tab=materials")).toBe(
      "/projects/1111-2222?tab=materials"
    );
  });

  test("strips credentials from an event's url, headers and breadcrumbs", () => {
    const event = scrubEvent({
      request: {
        url: "/accept-invitation?id=REAL-INVITE",
        headers: {
          Authorization: "Bearer real-token",
          Cookie: "refresh=abc",
          "User-Agent": "Mozilla/5.0",
        },
      },
      breadcrumbs: [
        { category: "fetch", data: { url: "/api/integrations/quickbooks/callback?code=REAL" } },
        { category: "navigation", data: { url: "/projects/abc" } },
      ],
    });

    expect(event.request.headers.Authorization).toBe("[redacted]");
    expect(event.request.headers.Cookie).toBe("[redacted]");
    // Benign headers and breadcrumbs survive.
    expect(event.request.headers["User-Agent"]).toBe("Mozilla/5.0");
    expect(event.breadcrumbs[1].data.url).toBe("/projects/abc");

    // The assertion that matters: no secret anywhere in the payload, not
    // merely absent from the field we happened to check.
    const payload = JSON.stringify(event);
    for (const secret of ["REAL-INVITE", "Bearer real-token", "refresh=abc"]) {
      expect(payload).not.toContain(secret);
    }
  });

  test("tolerates events missing the shapes it looks for", () => {
    // beforeSend runs on every event; throwing here would drop it.
    expect(scrubEvent({})).toEqual({});
    expect(scrubEvent({ request: null, breadcrumbs: undefined })).toBeTruthy();
  });
});
