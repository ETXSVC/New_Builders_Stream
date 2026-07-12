"""Legal Project status transitions (Task 1.13), table-driven per
[Test Strategy](../../docs/10-test-strategy.md) Section 4.

Transition table (explicit, not derived from adjacent-pair heuristics):

    draft             -> pre_construction
    pre_construction  -> active
    active            -> suspended, completed
    suspended         -> active, completed
    completed         -> archived
    archived          -> (terminal: no legal outgoing transition)

The linear spine comes straight from
[Functional Requirements](../../docs/02-functional-requirements.md) US-3.2's
stated pipeline order: "Draft -> Pre-Construction -> Active -> Suspended ->
Completed -> Archived." Same precedent as Task 1.5's Lead state machine:
skipping a stage from the START of the chain is illegal (e.g. `draft ->
completed`, `pre_construction -> suspended`) — but, also same as Task 1.5's
own precedent ("a lead can be lost from most stages... use judgment," which
this task's own text explicitly imports by name), `suspended` is treated as
a reversible SIDE DETOUR off of `active`, not a mandatory waypoint every
project must pass through to reach `completed`. `active -> completed` is
legal directly.

**Correction, found during this task's spec review**: an earlier version of
this table required `active -> suspended -> completed` as a mandatory
waypoint for every project, with no direct `active -> completed` edge. That
was wrong for the reason above — see the preceding paragraph for the
argument. Fixed by adding `active -> completed` directly.

The `suspended -> active` edge is the one addition the plan explicitly calls
for beyond the literal linear chain: "suspension needs to be reversible — the
linear list alone doesn't capture that." Without it, a suspended Project
would have no way back to `active` at all, which is clearly not the intent of
a "suspend/resume" mechanism. `suspended -> completed` is also legal, so a
suspended Project can either resume (`-> active`) or proceed to completion
(`-> completed`) directly, without needing to un-suspend first.

**Change Orders business rule — implemented, but not here (Task 2.23):**
[Functional Requirements](../../docs/02-functional-requirements.md) Section 3
states "A Project cannot move to Completed while it has open (non-approved)
Change Orders." This table-driven module stays pure data (no DB queries), so
that business rule is enforced one layer up, in
`update_project_status` (`app/routers/projects.py`) — same "state-machine
table stays pure, side-effect/business-rule checks live in the router" split
Task 1.18 established for the `LEAD_WON` event. Look there, not here, for the
actual enforcement logic (it queries for any `pending` Change Order against
the project and rejects with 409 before the transition is applied, gated on
`requested_status == "completed"` only — not on which status the project is
coming FROM, so it applies uniformly to both `active -> completed` and
`suspended -> completed`).

`archived` has no legal outgoing transition (terminal), same as Lead's
`won`/`lost`. `draft` has no legal incoming transition (it's the only status
a Project is ever created with — see `create_project` in
`app/routers/projects.py`), so it never appears as a value in any other
status's adjacency set.
"""

PROJECT_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"pre_construction"}),
    "pre_construction": frozenset({"active"}),
    "active": frozenset({"suspended", "completed"}),
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
