# Builders Stream — Product Requirements Document (PRD)

**Version:** 1.0
**Date:** 2026-07-07
**Status:** Draft for review

## 1. Executive Summary

Builders Stream is a multi-tenant SaaS platform for small/medium building construction and renovation companies. It unifies CRM, project management, cost estimation, accounting/billing, and company administration into a single system, targeting 1,000+ paying company subscribers on a self-hosted infrastructure.

## 2. Problem Statement

Small and medium construction/renovation contractors currently run their business across a fragmented set of disconnected tools: spreadsheets for estimating, a separate CRM (or none), email/text for client communication, and a bolt-on accounting package for invoicing. This fragmentation ("the fragmentation tax") causes:

- Lost or duplicated data entry between tools (a lead's details get re-typed into an estimate, then again into an invoice).
- No single source of truth for a project's financial state (estimated vs. actual cost).
- Manual, error-prone handoffs between sales (CRM), execution (project management), and billing.
- Difficulty tracking compliance obligations (subcontractor insurance, licenses, signed change orders) until a problem occurs.

## 3. Target Users / Personas

| Persona | Role | Primary Needs |
|---|---|---|
| Company Owner / Admin | Runs the company, owns the tenant | Visibility across all projects, financials, and branches; user/role management. |
| Project Manager | Manages day-to-day project execution | Task scheduling, document access, crew coordination, change order tracking. |
| Field Crew | On-site labor/subcontractors | Task lists, daily log submission, photo uploads. |
| Accountant / Bookkeeper | Manages financial records | Invoicing, expense tracking, QuickBooks/FreshBooks sync, financial reporting. |
| Client (external) | The homeowner/business paying for the project | Read-only progress visibility, estimate/change-order approval (e-signature). |

## 4. Goals & Success Metrics

| Goal | Metric |
|---|---|
| Reach commercial scale | 1,000 paying company (tenant) subscribers. |
| Strict tenant data isolation | Zero cross-tenant data leaks, verified by automated RLS isolation tests (see [Test Strategy](10-test-strategy.md)). |
| Reduce fragmentation | A lead can move from "New" through "Won" → active Project → approved Estimate → Invoice without re-entering data. |
| Operational reliability | 99.5%+ uptime on self-hosted infrastructure (see [NFRs](06-nonfunctional-requirements.md)). |

Note: precise adoption/revenue targets and conversion benchmarks are not yet defined and should be set once pricing (see [Pricing & Subscription Model](08-pricing-subscription-model.md)) is finalized against real market feedback — this PRD intentionally does not invent numbers that haven't been validated.

## 5. Product Scope

### 5.1 In Scope (Full Vision)

1. Users & Company Management (multi-tenant, nested company hierarchy, RBAC)
2. CRM (lead pipeline, client communication history)
3. Project Management (project lifecycle, tasks, documents, daily logs)
4. Estimation Engine (cost catalog, markup profiles, quote generation, e-signature approval)
5. Accounting & Billing (Stripe subscription billing, project invoicing)
6. External Integrations (QuickBooks / FreshBooks sync)
7. Compliance Tracking (subcontractor insurance/license expiry, signed change orders)

### 5.2 Out of Scope (v1)

- Native mobile apps (responsive web only; offline/PWA support is an open question — see Section 7).
- Payroll processing.
- Estimating via AI-driven blueprint takeoff (manual/assisted quantity entry only in v1).
- Multi-currency / multi-language support.

## 6. MVP Definition

The MVP is the minimum set of modules needed for a construction company to run its core sales-to-execution workflow inside Builders Stream:

- **Users & Company Management** (P0 — foundation, required by everything else)
- **CRM** (P1)
- **Project Management** (P1)
- **Estimation Engine**, including e-signature approval on estimates (P1/P2)

Accounting/Billing, QuickBooks/FreshBooks integration, and standalone insurance/license expiry tracking are explicitly **post-MVP** (see [Roadmap](09-roadmap-implementation-plan.md) for phasing and rationale).

## 7. Assumptions & Constraints

- **Team:** Solo developer, no hard external launch deadline — scope and pacing favor incremental delivery over a fixed date.
- **Hosting:** Self-hosted on the developer's own Proxmox/Dell PowerEdge infrastructure, not a managed cloud platform. This shapes the NFRs (Section 6) and deployment architecture.
- **Architecture:** Decoupled frontend/backend (Next.js + FastAPI) was chosen deliberately over a unified Next.js-only stack to preserve technology independence and support future non-web clients (mobile, integrations).
- **Offline/mobile field use:** Not yet decided. Flagged as an open question — if required later, it affects the Project Management module's sync architecture (see [Technical Architecture](03-technical-architecture.md), Section 8).

## 8. Open Questions

| # | Question | Owner | Status |
|---|---|---|---|
| 1 | Does field crew usage require offline-capable mobile/PWA support? | Product | Open |
| 2 | Final dollar pricing for Starter/Pro/Enterprise tiers | Product | Open — see [Pricing Model](08-pricing-subscription-model.md) |
| 3 | Target adoption/revenue metrics for the 1,000-subscriber goal | Product | Open |
