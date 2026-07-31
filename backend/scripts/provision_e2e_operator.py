"""Mint a signed-in-ready platform operator, for the e2e suite.

    python scripts/provision_e2e_operator.py --base-url http://localhost:8000

Prints three `KEY=value` lines on stdout — email, password and TOTP secret —
for a shell to `eval` or a CI job to append to `$GITHUB_ENV`.

WHY THIS EXISTS AT ALL. The console's authenticated surface went untested
because reaching it needs two things a Playwright spec cannot do for
itself: a `platform_admins` row, which only the table owner can write
(migration 0023, deliberately), and a live TOTP code, which needs the
enrollment secret. This script does both, from the one place that has
database access, and hands the spec a secret it can generate codes from.

WHY THE SPEC IS NOT GIVEN A TOKEN INSTEAD. It could be — and then the test
would prove nothing about the login it skipped. The spec signs in through
`POST /api/platform/auth/login` like an operator does, which is what puts
the httpOnly cookie in the browser context and makes the rest of the run
honest.

WHY ENROLLMENT HAPPENS HERE RATHER THAN IN THE SPEC. `/mfa/enroll` returns
409 once MFA is active, so a spec that enrolled itself would pass on the
first attempt and fail on a Playwright RETRY — the failure mode that looks
like flakiness and is not. Doing it once, here, leaves the spec with only
idempotent steps.

E2E/DEV ONLY. It refuses to run against `app_env=production`: the account
it creates has a known password and an operator-visible TOTP secret, which
is exactly what a real console account must never have.
"""
import argparse
import asyncio
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pyotp

from app.config import settings

DEFAULT_EMAIL = "e2e-operator@platform.example"
DEFAULT_PASSWORD = "E2E-Operator-Password-2026"


def _post(base_url: str, path: str, body: dict) -> tuple[int, dict | None]:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        raw = err.read().decode()
        try:
            return err.code, json.loads(raw) if raw else None
        except ValueError:
            return err.code, {"detail": raw}


async def provision(base_url: str, email: str, password: str) -> str:
    # 1. The user must exist before the grant: the grant script grants a
    #    privilege, it does not create a login.
    status, body = _post(
        base_url,
        "/auth/register",
        {
            "company_name": "E2E Operator Co",
            "admin_email": email,
            "admin_password": password,
            "admin_full_name": "E2E Operator",
        },
    )
    if status not in (200, 201) and status != 409:
        raise SystemExit(f"register failed: {status} {body}")

    # 2. The grant, through the real provisioning tool rather than raw SQL —
    #    so the path an operator is told to use is the path under test.
    grant = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("grant_platform_admin.py")), "grant", email],
        capture_output=True,
        text=True,
    )
    if grant.returncode != 0:
        raise SystemExit(f"grant failed: {grant.stdout}{grant.stderr}")

    # 3. Enrollment. Refused with 409 if this account already has MFA
    #    active, which is why the caller gets a fresh email by default.
    status, body = _post(base_url, "/platform/auth/mfa/enroll", {"email": email, "password": password})
    if status != 200 or not body:
        raise SystemExit(
            f"mfa/enroll failed: {status} {body}. If this is 409, the account already has "
            "MFA active and its secret is unrecoverable — provision a different email."
        )
    secret = body["secret"]

    # 4. Activate. Do not start inside the last few seconds of a timestep:
    #    the code would be computed for one step and verified in the next.
    if 30 - int(time.time()) % 30 < 5:
        time.sleep(5)
    status, body = _post(
        base_url,
        "/platform/auth/mfa/activate",
        {"email": email, "password": password, "totp_code": pyotp.TOTP(secret).now()},
    )
    if status != 204:
        raise SystemExit(f"mfa/activate failed: {status} {body}")

    return secret


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000", help="backend base URL")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    args = parser.parse_args()

    if settings.app_env == "production":
        print("Refusing to run with app_env=production.", file=sys.stderr)
        return 2

    secret = await provision(args.base_url, args.email, args.password)

    print(f"E2E_PLATFORM_EMAIL={args.email}")
    print(f"E2E_PLATFORM_PASSWORD={args.password}")
    print(f"E2E_PLATFORM_TOTP_SECRET={secret}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
