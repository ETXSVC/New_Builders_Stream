"""Dispatcher-mechanics tests for app.core.events (Task 1.6).

app/core/events.py itself was already built during Task 1.5 (and extended
with clear() during that task's code-review follow-up) because PATCH
/leads/{id} needed a real, callable publish() before this task ran — see the
module's own docstring for the full scope-split explanation. This file
covers the dispatcher mechanics the module's docstring and Task 1.6's plan
text call out: register + publish delivers the right payload, multiple
handlers fire in registration order, an unhandled event name is a no-op, a
handler's exception propagates to the caller (and stops later handlers for
that event from running), different event names don't cross-fire, and
clear() actually empties the registry.

conftest.py's autouse `_clean_event_registry` fixture already calls
events.clear() before and after every test, so no manual registry cleanup is
needed here.
"""

import pytest

from app.core import events


async def test_publish_calls_single_handler_with_payload():
    received = {}

    async def handler(**payload):
        received.update(payload)

    events.register("LEAD_WON", handler)
    await events.publish("LEAD_WON", lead_id="lead-1", company_id="company-1")

    assert received == {"lead_id": "lead-1", "company_id": "company-1"}


async def test_publish_calls_multiple_handlers_in_registration_order():
    call_order = []

    async def first(**payload):
        call_order.append("first")

    async def second(**payload):
        call_order.append("second")

    async def third(**payload):
        call_order.append("third")

    events.register("LEAD_WON", first)
    events.register("LEAD_WON", second)
    events.register("LEAD_WON", third)
    await events.publish("LEAD_WON")

    assert call_order == ["first", "second", "third"]


async def test_publish_with_no_registered_handlers_is_noop():
    # No handler registered for this event name at all — must not raise.
    await events.publish("SOME_UNSUBSCRIBED_EVENT", anything="here")


async def test_publish_propagates_handler_exception():
    async def failing_handler(**payload):
        raise ValueError("handler blew up")

    events.register("LEAD_WON", failing_handler)

    with pytest.raises(ValueError, match="handler blew up"):
        await events.publish("LEAD_WON")


async def test_publish_stops_after_first_handler_raises():
    second_handler_called = False

    async def first_handler(**payload):
        raise RuntimeError("first handler fails")

    async def second_handler(**payload):
        nonlocal second_handler_called
        second_handler_called = True

    events.register("LEAD_WON", first_handler)
    events.register("LEAD_WON", second_handler)

    with pytest.raises(RuntimeError, match="first handler fails"):
        await events.publish("LEAD_WON")

    assert second_handler_called is False


async def test_publish_does_not_cross_fire_different_event_names():
    a_called = False
    b_called = False

    async def handler_a(**payload):
        nonlocal a_called
        a_called = True

    async def handler_b(**payload):
        nonlocal b_called
        b_called = True

    events.register("EVENT_A", handler_a)
    events.register("EVENT_B", handler_b)
    await events.publish("EVENT_A")

    assert a_called is True
    assert b_called is False


async def test_clear_empties_the_registry():
    handler_called = False

    async def handler(**payload):
        nonlocal handler_called
        handler_called = True

    events.register("LEAD_WON", handler)
    events.clear()
    await events.publish("LEAD_WON")

    assert handler_called is False
