# Builders Stream

A multi-tenant SaaS platform for small/medium building construction and renovation companies, unifying CRM, project management, cost estimation, accounting/billing, and company administration into a single system.

**Status:** Planning / documentation phase — no application code has been written yet. This repository currently holds the requirements, architecture, and design documentation that implementation will follow.

## Documentation

The full document set lives in [`docs/`](docs/) and a combined, presentation-ready copy is at [`Builders Stream.docx`](Builders%20Stream.docx) (title page, table of contents, and diagrams).

| Doc | Contents |
|---|---|
| [01-prd.md](docs/01-prd.md) | Product vision, personas, goals, MVP boundary, open questions |
| [02-functional-requirements.md](docs/02-functional-requirements.md) | User stories + acceptance criteria for every module |
| [03-technical-architecture.md](docs/03-technical-architecture.md) | Stack, modular monolith design, tenant/RLS model, deployment topology |
| [04-database-schema.md](docs/04-database-schema.md) | Full ERD and SQL schema |
| [05-api-specification.md](docs/05-api-specification.md) | Conceptual REST endpoint contracts per module |
| [06-nonfunctional-requirements.md](docs/06-nonfunctional-requirements.md) | Performance, scalability, availability, backup, infra sizing |
| [07-security-compliance.md](docs/07-security-compliance.md) | RBAC matrix, encryption, audit logging, e-signature, retention |
| [08-pricing-subscription-model.md](docs/08-pricing-subscription-model.md) | Tiered pricing structure and Stripe billing model |
| [09-roadmap-implementation-plan.md](docs/09-roadmap-implementation-plan.md) | Phased build plan (Phase 0–5), MVP launch bar |
| [10-test-strategy.md](docs/10-test-strategy.md) | Test pyramid, tenant-isolation release gate, test cases |

Start with the [PRD](docs/01-prd.md) for the "why," then [Technical Architecture](docs/03-technical-architecture.md) for the "how."

## Planned Architecture (Summary)

- **Frontend:** Next.js 16 (App Router), TypeScript
- **Backend:** FastAPI (Python 3.12), decoupled from the frontend, organized as a modular monolith
- **Database:** PostgreSQL with Row-Level Security for multi-tenant isolation, including a nested (parent/child branch) company hierarchy
- **Background jobs:** Celery/Dramatiq + Redis (PDF generation, QuickBooks/FreshBooks sync, notifications)
- **Payments:** Stripe (Builders Stream's own subscription billing)
- **Hosting:** Self-hosted (Docker Compose on the developer's own Proxmox infrastructure)

Full rationale and diagrams are in [03-technical-architecture.md](docs/03-technical-architecture.md).

## MVP Scope

Users & Company Management → CRM → Project Management → Estimation Engine (with e-signature approval). Accounting/Billing, QuickBooks/FreshBooks integration, and standalone compliance tracking are post-MVP. See [09-roadmap-implementation-plan.md](docs/09-roadmap-implementation-plan.md) for the full phase breakdown.

## Open Questions

Tracked in [01-prd.md](docs/01-prd.md#8-open-questions):

- Whether field crews need offline/mobile (PWA) support
- Final dollar pricing for the Starter/Pro/Enterprise tiers
- Target adoption/revenue metrics

## Contributing / Next Steps

No code exists yet. The next step is turning [Phase 0 (Foundation)](docs/09-roadmap-implementation-plan.md#phase-0--foundation-pre-mvp) into a concrete implementation plan.
