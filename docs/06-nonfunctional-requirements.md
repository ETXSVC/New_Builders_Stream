# Builders Stream — Non-Functional Requirements

**Version:** 1.0
**Date:** 2026-07-07
**Related:** [Technical Architecture](03-technical-architecture.md) · [Security & Compliance](07-security-compliance.md)

## 1. Performance

| Requirement | Target |
|---|---|
| API response time (p95, non-report endpoints) | < 300ms under normal load |
| Estimate recalculation (`/estimates/{id}/calculate`) | < 1s for estimates up to 200 line items |
| PDF proposal generation | Asynchronous; queued job completes within 30s |
| Dashboard initial load (frontend) | < 2s time-to-interactive on broadband |

## 2. Scalability

- Must support **1,000+ company tenants** on shared-schema PostgreSQL without per-tenant infrastructure.
- Indexing strategy (see [Database Schema](04-database-schema.md), Section 9) must keep core list/detail queries performant as row counts grow into the millions.
- **Trigger for revisiting the recursive RLS lookup:** if `EXPLAIN ANALYZE` on `get_all_descendant_ids` shows measurable latency (baseline: establish this once real company-tree depth/fan-out data exists — do not pre-optimize before that), cache the permitted tenant-ID set in the session/JWT instead of recomputing per request.
- **Trigger for extracting a module into its own service:** a module's background job queue depth or CPU usage disproportionately dominates the shared backend's resource budget, as observed via the Section 5 monitoring below — not before.

## 3. Availability

- Target **99.5% uptime** for the self-hosted production environment (roughly 3.6 hours of downtime/month budget) — a realistic target for a single-operator self-hosted deployment, not a marketing claim.
- Planned maintenance windows are acceptable and should be communicated in-app in advance; they do not count against the target if scheduled outside business hours and announced.
- No multi-region failover in v1 — this is a single self-hosted environment. Documented as a known limitation, not a gap to silently ignore.

## 4. Backup & Disaster Recovery

- PostgreSQL: automated daily full backups + continuous WAL archiving, retained 30 days, stored off the Proxmox host (a separate physical or cloud location).
- Document/file storage: mirrored to a secondary location on the same backup cadence.
- Recovery Point Objective (RPO): ≤ 24 hours (bounded by backup cadence; WAL archiving can reduce this further once implemented).
- Recovery Time Objective (RTO): documented and tested via an actual restore drill before the platform accepts its first paying subscriber — not assumed to work untested.

## 5. Observability

- **Error tracking:** Sentry (self-hosted or cloud) on both frontend and backend.
- **Product analytics:** PostHog for feature usage, funnel tracking (Lead → Won → Estimate Approved).
- **Infrastructure monitoring:** container health, CPU/memory/disk on the Proxmox host, PostgreSQL connection pool saturation, Redis/Celery queue depth — minimum viable setup is Prometheus + Grafana or an equivalent self-hosted stack, since there is no managed-cloud dashboard to rely on.
- **Alerting:** on-call notification (email/SMS/push) for: service down, backup failure, disk usage above 85%, queue depth exceeding a defined threshold.

## 6. Self-Hosted Infrastructure Requirements

- Minimum viable production footprint: one Proxmox VM (or set of VMs) capable of running the Docker Compose stack in [Technical Architecture](03-technical-architecture.md), Section 8 — exact CPU/RAM/storage sizing should be benchmarked against real load rather than assumed, given the existing Dell PowerEdge T620 hardware's known specs are not detailed in this document.
- Network: static IP or dynamic DNS, valid TLS certificate (Let's Encrypt via the reverse proxy), firewall rules restricting direct database/Redis access to the internal Docker network only.
- Staging environment mirrors production topology at smaller scale, used for pre-deployment verification (see [Test Strategy](10-test-strategy.md)).

## 7. Browser & Device Support

- Modern evergreen browsers (Chrome, Edge, Safari, Firefox — current and prior major version).
- Responsive layout down to tablet width (768px) as a baseline; phone-width usability is best-effort in v1 pending the mobile/offline decision noted in [PRD](01-prd.md), Section 7.

## 8. Data Retention

- See [Security & Compliance](07-security-compliance.md), Section 7 for retention rules specific to e-signature and audit-log records, which have longer/immutable retention requirements than general operational data.
