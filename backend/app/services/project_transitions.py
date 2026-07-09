"""Legal Project status transitions (Task 1.13), table-driven per
[Test Strategy](../../docs/10-test-strategy.md) Section 4.

Transition table (explicit, not derived from adjacent-pair heuristics):

    draft             -> pre_construction
    pre_construction  -> active
    active            -> suspended
    suspended         -> active, completed
    completed         -> archived
    archived          -> (terminal: no legal outgoing transition)

The linear spine comes straight from
[Functional Requirements](../../docs/02-functional-requirements.md) US-3.2's
stated pipeline order: "Draft -> Pre-Construction -> Active -> Suspended ->
Completed -> Archived." Same precedent as Task 1.5's Lead state machine:
skipping a stage (e.g. `draft -> completed`, `pre_construction -> suspended`,
or `active -> completed`) is illegal — only the single-step edges of the
literal pipeline are legal, so a Project must pass through every stage in
order, INCLUDING `suspended` on the way to `completed`. This is a deliberate,
literal reading of US-3.2's own chain (which places `suspended` directly
before `completed`) rather than treating `suspended` as an optional side
detour off of `active` — the plan text's only called-out gap in the linear
list is reversibility (see below), not a shortcut around `suspended`. A
project that is never suspended therefore cannot reach `completed` per this
reading; that's a real-world-surprising business rule worth revisiting with
product in a later phase, but it's what US-3.2's literal chain states and
matches Task 1.5's own "no skipping stages" discipline, so it's implemented
as written rather than silently "fixed."

The `suspended -> active` edge is the one addition the plan explicitly calls
for beyond the literal linear chain: "suspension needs to be reversible — the
linear list alone doesn't capture that." Without it, a suspended Project
would have no way back to `active` at all, which is clearly not the intent of
a "suspend/resume" mechanism. `suspended -> completed` (already part of the
literal chain) is kept as-is, so a suspended Project can either resume
(`-> active`) or proceed to completion (`-> completed`) directly, without
needing to un-suspend first.

**Change Orders business rule — deferred, not implemented:**
[Functional Requirements](../../docs/02-functional-requirements.md) Section 3
states "A Project cannot move to Completed while it has open (non-approved)
Change Orders." This is NOT enforced here: `change_orders` doesn't exist as a
table/model in Phase 1 (explicitly out of scope — see the top of the Phase 1
plan doc). When Change Orders ships in Phase 2, the `suspended -> completed`
transition (and any other future edge that lands on `completed`) needs an
additional application-layer check — querying for any non-approved Change
Order rows against this project and rejecting the transition (409) if any
exist — layered on top of (not replacing) the table-driven check below. Do
not forget to add this when Change Orders lands; nothing here enforces it
yet.

`archived` has no legal outgoing transition (terminal), same as Lead's
`won`/`lost`. `draft` has no legal incoming transition (it's the only status
a Project is ever created with — see `create_project` in
`app/routers/projects.py`), so it never appears as a value in any other
status's adjacency set.
"""

PROJECT_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"pre_construction"}),
    "pre_construction": frozenset({"active"}),
    "active": frozenset({"suspended"}),
    "suspended": frozenset({"active", "completed"}),
    "completed": frozenset({"archived"}),
    "archived": frozenset(),
}


def is_legal_transition(current_status: str, new_status: str) -> bool:
    """True if `current_status -> new_status` is a legal single-step Project
    status transition per PROJECT_TRANSITIONS above. Callers are expected to
    handle the same-status (no-op) case themselves before consulting this —
    this function has no opinion on self-transitions beyond the fact that no
    status appears in its own adjacency set, so `is_legal_transition(x, x)`
    is always False."""
    return new_status in PROJECT_TRANSITIONS.get(current_status, frozenset())
