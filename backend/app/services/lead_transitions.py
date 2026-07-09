"""Legal Lead status transitions (Task 1.5), table-driven per
[Test Strategy](../../docs/10-test-strategy.md) Section 4.

Transition table (explicit, not derived from adjacent-pair heuristics):

    new        -> contacted, lost
    contacted  -> estimating, lost
    estimating -> qualified, lost
    qualified  -> won, lost
    won        -> (terminal: no legal outgoing transition)
    lost       -> (terminal: no legal outgoing transition)

The linear spine (new -> contacted -> estimating -> qualified -> won) comes
straight from [Functional Requirements](../../docs/02-functional-requirements.md)
US-2.2's stated pipeline order. Skipping a stage (e.g. `new -> won` or
`new -> qualified`) is illegal — only the single-step edges above are legal,
so a Lead must pass through every stage to reach `won`.

The "-> lost" fan-out is a judgment call the plan explicitly flags as
needing one ("a lead can be lost from most stages, not just qualified...
use judgment"): every *non-terminal* stage (`new`, `contacted`, `estimating`,
`qualified`) can transition directly to `lost`. `won` is deliberately
excluded from the fan-out — once a Lead has converted (triggering the
`LEAD_WON` event and a draft Project), un-winning it into `lost` would leave
a dangling drafted Project with no corresponding real business action, and
nothing in the functional-requirements doc describes reverting a won Lead.
`lost` is excluded from its own fan-out because `lost` is itself terminal:
re-transitioning `lost -> lost` isn't a real transition and is already
rejected by the empty adjacency set below (no self-loops are modeled — a
same-status PATCH is handled upstream in the router as a no-op, not routed
through this table at all).
"""

LEAD_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"contacted", "lost"}),
    "contacted": frozenset({"estimating", "lost"}),
    "estimating": frozenset({"qualified", "lost"}),
    "qualified": frozenset({"won", "lost"}),
    "won": frozenset(),
    "lost": frozenset(),
}


def is_legal_transition(current_status: str, new_status: str) -> bool:
    """True if `current_status -> new_status` is a legal single-step Lead
    status transition per LEAD_TRANSITIONS above. Callers are expected to
    handle the same-status (no-op) case themselves before consulting this —
    this function has no opinion on self-transitions beyond the fact that
    no status appears in its own adjacency set, so `is_legal_transition(x, x)`
    is always False."""
    return new_status in LEAD_TRANSITIONS.get(current_status, frozenset())
