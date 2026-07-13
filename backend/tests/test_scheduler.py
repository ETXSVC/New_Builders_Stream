"""Task 3.9: `app/scheduler.py`'s `_run_check_compliance_expiry` wiring.

Uses pytest's own built-in `monkeypatch` fixture, not a mocking library —
this codebase has no established mocking precedent anywhere (its own test
strategy explicitly favors real dependencies over mocks that can silently
drift from production behavior, see docs/10-test-strategy.md), and
`settings.redis_url` is NOT split into a dev/test pair the way
`DATABASE_URL`/`TEST_DATABASE_URL` are — sending a genuine message via
`check_compliance_expiry.send()` in an automated test would enqueue onto
the SAME Redis queue namespace a real dev worker process could be
consuming from, an unacceptable side effect for a test that runs
routinely. `monkeypatch` narrowly replaces just the one attribute this
test needs to observe, for the duration of this one test only, without
touching real Redis at all — the live end-to-end message flow (scheduler
enqueues -> worker dequeues -> actor runs -> DB row written) was already
verified for real against an isolated Docker Compose stack during this
task's own spec-compliance review; this test locks in the wiring so a
future edit (e.g. accidentally calling `_check_compliance_expiry` directly
instead of `.send()`) fails fast in CI rather than silently at 2am.
"""

from app.scheduler import _run_check_compliance_expiry
from app.tasks.compliance_expiry import check_compliance_expiry


def test_run_check_compliance_expiry_calls_send_not_the_undecorated_function(monkeypatch):
    calls = []
    monkeypatch.setattr(check_compliance_expiry, "send", lambda *a, **kw: calls.append((a, kw)))

    _run_check_compliance_expiry()

    assert calls == [((), {})]
