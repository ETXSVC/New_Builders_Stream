# Database ERD

Generated from the **live schema** — a scratch database with all 34 Alembic
migrations applied — rather than from the ORM models or by hand, so it cannot
quietly drift from what `alembic upgrade head` actually produces.

**46 tables** (excluding `alembic_version`). Last re-derived 2026-08-02
against migration `0033`; the counts and the no-RLS list below come from
`pg_class`/`pg_attribute`, not from memory. Migrations `0034` and `0035`
added columns and no tables, so the counts stand — `daily_logs.client_reference`
and `estimate_line_items.description`/`.unit` are reflected in their blocks
below.

## Read this first: the tenant boundary is in the database

The single most important property of this schema is not visible in an
ordinary ERD: **PostgreSQL Row-Level Security is the tenant boundary**, not
application code. Every tenant table carries `company_id` and a `FOR ALL`
policy scoped by `get_all_descendant_ids(current_setting('app.current_tenant'))`,
so a query that forgets to filter by company still cannot cross tenants.

- RLS enabled: **43 of 46** tables
- Deliberately without RLS: `refresh_tokens`, `users`, `password_reset_tokens`

`users` is global by design (one person may belong to several companies, and
login happens before any tenant is known); `refresh_tokens` hangs off `users`
and is looked up by token hash, never by tenant.

`platform_admins` (migration 0023) is the one table with RLS enabled but **no
`company_id`** — a platform administrator belongs to no tenant, so the usual
policy would be meaningless. Its policy is scoped to the caller's own user id
instead, so the request path can ask "am I a platform admin?" without being
able to enumerate who else is. See [Database Schema](04-database-schema.md),
Section 9.

`companies.parent_id` self-references to form the branch hierarchy, and is
**immutable** — migration 0021 enforces that with a trigger, because
re-parenting would move a subtree between tenants and detach it from its
subscription.

## Tenancy spine

```mermaid
erDiagram
    companies {
        uuid id "PK"
        uuid parent_id "FK"
        varchar name
        bool is_active
        timestamptz created_at
    }
    company_users {
        uuid company_id "PK,FK"
        uuid user_id "PK,FK"
        varchar role
        timestamptz created_at
    }
    users {
        uuid id "PK"
        varchar email
        varchar password_hash
        varchar full_name
        timestamptz created_at
        text totp_secret_encrypted
        timestamptz mfa_activated_at
        bigint totp_last_used_step
    }
    companies ||--o{ companies : "parent_id (immutable)"
    companies ||--o{ company_users : "company_id"
    users ||--o{ company_users : "user_id"
```

Membership is the join: a user's role is per-company, so the same person can
be an admin of one branch and a client of another.

## Tenancy, identity and access

> No RLS on: `users`, `refresh_tokens`, `password_reset_tokens` — see above.
> A reset is requested with no session, no `X-Tenant-ID` and no membership
> resolved, so there is no tenant to scope the row to; the credential is
> protected by being a SHA-256 of a secret that lives in one email
> (migration 0028).

```mermaid
erDiagram
    companies {
        uuid id "PK"
        uuid parent_id "FK"
        varchar name
        bool is_active
        timestamptz created_at
    }
    company_users {
        uuid company_id "PK,FK"
        uuid user_id "PK,FK"
        varchar role
        timestamptz created_at
    }
    users {
        uuid id "PK"
        varchar email
        varchar password_hash
        varchar full_name
        timestamptz created_at
        text totp_secret_encrypted
        timestamptz mfa_activated_at
        bigint totp_last_used_step
    }
    invitations {
        uuid id "PK"
        uuid company_id "FK"
        varchar email
        varchar role
        timestamptz expires_at
        timestamptz accepted_at
    }
    refresh_tokens {
        uuid id "PK"
        uuid user_id "FK"
        varchar token_hash
        uuid family_id
        timestamptz issued_at
        timestamptz expires_at
        timestamptz revoked_at
        uuid replaced_by_id "FK"
    }
    audit_log {
        uuid id "PK"
        uuid company_id "FK"
        uuid actor_id "FK"
        varchar action
        varchar entity_type
        uuid entity_id
        jsonb log_metadata
        timestamptz created_at
    }
    company_branding {
        uuid id "PK"
        uuid company_id "FK"
        text logo_storage_path
        varchar accent_color
        text footer_text
        timestamptz updated_at
    }
    subscriptions {
        uuid id "PK"
        uuid company_id "FK"
        varchar stripe_customer_id
        varchar stripe_subscription_id
        varchar tier
        varchar status
        int included_seats
        timestamptz current_period_end
    }

    users ||--o{ audit_log : "actor_id"
    companies ||--o{ audit_log : "company_id"
    companies ||--o{ companies : "parent_id"
    companies ||--o{ company_branding : "company_id"
    companies ||--o{ company_users : "company_id"
    users ||--o{ company_users : "user_id"
    companies ||--o{ invitations : "company_id"
    refresh_tokens ||--o{ refresh_tokens : "replaced_by_id"
    users ||--o{ refresh_tokens : "user_id"
    companies ||--o{ subscriptions : "company_id"
```

## CRM — leads

```mermaid
erDiagram
    leads {
        uuid id "PK"
        uuid company_id "FK"
        varchar contact_name
        varchar project_name
        varchar email
        varchar phone
        varchar status
        numeric estimated_value
        varchar project_type
        text notes
        timestamptz created_at
        timestamptz updated_at
    }
    communication_logs {
        uuid id "PK"
        uuid lead_id "FK"
        uuid company_id "FK"
        uuid author_id "FK"
        varchar channel
        text body
        timestamptz created_at
    }
    lead_clients {
        uuid id "PK"
        uuid company_id "FK"
        uuid user_id "FK"
        uuid lead_id "FK"
        timestamptz created_at
    }

    leads ||--o{ communication_logs : "lead_id"
    leads ||--o{ lead_clients : "lead_id"
```

## Project management

```mermaid
erDiagram
    projects {
        uuid id "PK"
        uuid company_id "FK"
        uuid lead_id "FK"
        varchar name
        text site_address
        varchar status
        date projected_start_date
        timestamptz created_at
        timestamptz updated_at
    }
    phases {
        uuid id "PK"
        uuid project_id "FK"
        uuid company_id "FK"
        varchar name
        int sequence
    }
    tasks {
        uuid id "PK"
        uuid phase_id "FK"
        uuid company_id "FK"
        varchar name
        uuid assignee_id "FK"
        date due_date
        varchar status
        timestamptz created_at
    }
    documents {
        uuid id "PK"
        uuid project_id "FK"
        uuid company_id "FK"
        varchar file_name
        text storage_path
        int version
        uuid uploaded_by "FK"
        timestamptz created_at
    }
    daily_logs {
        uuid id "PK"
        uuid project_id "FK"
        uuid company_id "FK"
        uuid author_id "FK"
        date log_date
        varchar weather
        text notes
        uuid client_reference "offline queue idempotency key"
        timestamptz created_at
    }
    project_clients {
        uuid id "PK"
        uuid company_id "FK"
        uuid user_id "FK"
        uuid project_id "FK"
        timestamptz created_at
    }

    projects ||--o{ daily_logs : "project_id"
    projects ||--o{ documents : "project_id"
    projects ||--o{ phases : "project_id"
    projects ||--o{ project_clients : "project_id"
    phases ||--o{ tasks : "phase_id"
```

## Estimation and e-signature

```mermaid
erDiagram
    cost_catalog_items {
        uuid id "PK"
        uuid company_id "FK"
        uuid parent_catalog_item_id "FK"
        varchar category
        varchar name
        varchar unit
        numeric unit_rate
        timestamptz updated_at
    }
    markup_profiles {
        uuid id "PK"
        uuid company_id "FK"
        varchar name
        numeric overhead_pct
        numeric profit_pct
    }
    estimates {
        uuid id "PK"
        uuid company_id "FK"
        uuid project_id "FK"
        uuid lead_id "FK"
        uuid markup_profile_id "FK"
        varchar status
        numeric subtotal
        numeric total
        bool is_snapshotted
        uuid esignature_id "FK"
        varchar pdf_status
        text pdf_storage_path
        timestamptz pdf_generated_at
        timestamptz created_at
        timestamptz updated_at
    }
    estimate_line_items {
        uuid id "PK"
        uuid estimate_id "FK"
        uuid company_id "FK"
        uuid cost_catalog_item_id "FK, null on a free-form line"
        varchar description "free-form lines only"
        varchar unit "free-form lines only"
        numeric quantity
        numeric unit_rate_snapshot
        numeric line_total
    }
    esignatures {
        uuid id "PK"
        uuid company_id "FK"
        varchar signer_name
        varchar signer_email
        timestamptz signed_at
        inet ip_address
        text signature_artifact_path
        varchar document_type
        uuid signed_by_user_id "FK"
    }
    change_orders {
        uuid id "PK"
        uuid project_id "FK"
        uuid company_id "FK"
        text description
        numeric cost_delta
        int schedule_impact_days
        varchar status
        uuid esignature_id "FK"
        timestamptz created_at
    }

    esignatures ||--o{ change_orders : "esignature_id"
    cost_catalog_items ||--o{ cost_catalog_items : "parent_catalog_item_id"
    cost_catalog_items ||--o{ estimate_line_items : "cost_catalog_item_id"
    estimates ||--o{ estimate_line_items : "estimate_id"
    esignatures ||--o{ estimates : "esignature_id"
    markup_profiles ||--o{ estimates : "markup_profile_id"
```

## Bill of Materials

```mermaid
erDiagram
    vendors {
        uuid id "PK"
        uuid company_id "FK"
        varchar name
        varchar contact_email
        varchar contact_phone
        varchar notes
        timestamptz created_at
        timestamptz updated_at
    }
    bom_lines {
        uuid id "PK"
        uuid company_id "FK"
        uuid project_id "FK"
        uuid cost_catalog_item_id "FK"
        uuid vendor_id "FK"
        varchar description
        varchar unit
        numeric quantity
        bool ordered
        timestamptz ordered_at
        varchar source
        timestamptz created_at
        timestamptz updated_at
    }
    bom_line_receipts {
        uuid id "PK"
        uuid bom_line_id "FK"
        uuid company_id "FK"
        numeric quantity
        timestamptz received_at
        uuid recorded_by_user_id "FK"
    }

    bom_lines ||--o{ bom_line_receipts : "bom_line_id"
    vendors ||--o{ bom_lines : "vendor_id"
```

## Compliance tracking

```mermaid
erDiagram
    subcontractors {
        uuid id "PK"
        uuid company_id "FK"
        varchar name
        varchar trade
        varchar contact_email
        timestamptz created_at
    }
    subcontractor_assignments {
        uuid id "PK"
        uuid project_id "FK"
        uuid subcontractor_id "FK"
        uuid company_id "FK"
        uuid assigned_by "FK"
        text override_reason
        timestamptz created_at
    }
    compliance_documents {
        uuid id "PK"
        uuid subcontractor_id "FK"
        uuid company_id "FK"
        varchar doc_type
        text storage_path
        date expires_on
        timestamptz created_at
    }
    compliance_notifications {
        uuid id "PK"
        uuid company_id "FK"
        uuid compliance_document_id "FK"
        varchar threshold
        timestamptz fired_at
        timestamptz read_at
    }

    subcontractors ||--o{ compliance_documents : "subcontractor_id"
    compliance_documents ||--o{ compliance_notifications : "compliance_document_id"
    subcontractors ||--o{ subcontractor_assignments : "subcontractor_id"
```

## Invoicing, AP and expenses

```mermaid
erDiagram
    invoices {
        uuid id "PK"
        uuid project_id "FK"
        uuid company_id "FK"
        uuid estimate_id "FK"
        varchar invoice_number
        numeric amount
        varchar status
        date due_date
        timestamptz created_at
    }
    invoice_payments {
        uuid id "PK"
        uuid invoice_id "FK"
        uuid company_id "FK"
        numeric amount
        date paid_date
        uuid recorded_by "FK"
        timestamptz created_at
    }
    bills {
        uuid id "PK"
        uuid company_id "FK"
        uuid project_id "FK"
        uuid subcontractor_id "FK"
        varchar vendor_name
        varchar bill_number
        numeric amount
        varchar status
        date due_date
        timestamptz created_at
    }
    bill_payments {
        uuid id "PK"
        uuid bill_id "FK"
        uuid company_id "FK"
        numeric amount
        date paid_date
        uuid recorded_by "FK"
        timestamptz created_at
    }
    expenses {
        uuid id "PK"
        uuid project_id "FK"
        uuid company_id "FK"
        varchar description
        numeric amount
        date incurred_on
    }

    bills ||--o{ bill_payments : "bill_id"
    invoices ||--o{ invoice_payments : "invoice_id"
```

## Accounting integrations

```mermaid
erDiagram
    integration_connections {
        uuid id "PK"
        uuid company_id "FK"
        varchar provider
        text access_token_encrypted
        text refresh_token_encrypted
        timestamptz connected_at
        varchar provider_account_id "nullable (0030)"
    }
    integration_entity_mappings {
        uuid id "PK"
        uuid company_id "FK"
        uuid connection_id "FK"
        varchar entity_kind
        varchar local_key
        varchar provider_entity_id
        timestamptz created_at
        timestamptz updated_at
    }
    integration_sync_records {
        uuid id "PK"
        uuid company_id "FK"
        uuid connection_id "FK"
        varchar entity_type
        uuid entity_id
        varchar status
        int attempt_count
        text last_error
        timestamptz last_attempted_at
        timestamptz created_at
        varchar external_record_id
    }

    integration_connections ||--o{ integration_sync_records : "connection_id"
    integration_connections ||--o{ integration_entity_mappings : "connection_id"
```

`integration_entity_mappings` (migration 0030) is what a *real* provider
needed and the fake never did: QuickBooks and FreshBooks each mint their own
id for a customer or vendor, and a second sync of the same entity must reuse
it rather than create a duplicate. Unique on
`(connection_id, entity_kind, local_key)`.

`local_key` is deliberately **the display name that was matched on, not a
foreign key** — what is being mapped is not always a row in this database. A
bill's vendor is free text on `bills.vendor_name`, and an expense account is
a provider-side concept with no local counterpart at all. For the same
reason it is one table with an `entity_kind` discriminator rather than three
(`customers`/`vendors`/`accounts`): identical shape, identical lookup, so
three tables would be three copies of the same policy, index and upsert.

`provider_account_id` on `integration_connections` is the realm/account the
tokens were issued for — nullable because the fake has no notion of one.

## Tenant financial settings (migration 0033)

```mermaid
erDiagram
    companies {
        uuid id "PK"
    }
    company_financial_settings {
        uuid id "PK"
        uuid company_id "FK"
        numeric deposit_percentage "nullable"
        numeric tax_rate "nullable"
        timestamptz created_at
        timestamptz updated_at
    }

    companies ||--o| company_financial_settings : "company_id"
```

At most one row per company, holding the two rates that were previously
module constants in `app/services/invoicing.py`: the deposit percentage
`ESTIMATE_APPROVED` uses when it drafts a deposit invoice, and the tax rate
the profitability report applies to estimated liability. `numeric(6,5)`
stores a rate, not a percentage — 0.08250 is 8.25%.

Both columns are nullable and resolved **independently, per value**: the
company's own setting, else its **root** company's, else the code default
(10% deposit, 0% tax). Two properties follow, and both are deliberate:

- **Per value, not per row.** A tenant may want to state a deposit policy
  and leave tax alone; resolving the whole row would make setting one
  silently adopt the other's default.
- **Root fallback, not plain per-company.** A head office sets a policy once
  and branches follow it, while a branch in another state can still
  override. This is deliberately *not* the root-only resolution
  `subscriptions` uses — a subscription genuinely belongs to the root, but a
  tax rate is exactly the thing a branch needs to differ on.

Changing a rate does not rewrite history: a deposit invoice's amount is
computed at approval and stored, so invoices already raised keep the rate
they were agreed at. The report's tax figure *is* recomputed live, because
it is labelled an estimate of current liability rather than a record.

## Platform administration

The one group that sits *above* the tenant hierarchy rather than inside it.
Neither table is writable by `app_user` or `scanner` — migration 0023 revokes
`INSERT/UPDATE/DELETE` on both — so no HTTP request or background job can
grant platform privilege or edit an entitlement.

```mermaid
erDiagram
    platform_admins {
        uuid id "PK"
        uuid user_id "FK — no company_id: belongs to no tenant"
        uuid granted_by "FK"
        timestamptz granted_at
        timestamptz revoked_at "NULL = active; re-checked every request"
        text note
    }
    company_module_overrides {
        uuid id "PK"
        uuid company_id "FK — always a ROOT company"
        varchar module
        boolean enabled "true grants, false withholds, no row defers to tier"
        text note
        uuid set_by "FK"
        timestamptz created_at
    }

    users ||--o{ platform_admins : "user_id"
    companies ||--o{ company_module_overrides : "company_id"
```

`enabled` is three-state on purpose, and the middle state is why the column
cannot simply be a boolean flag on `subscriptions`: **no row** means "use the
tier", `true` grants a module the tier would withhold, and `false` withholds
one the tier would grant. Collapsing the first two would make "off"
unexpressible.

## Team directory (migration 0026)

> Each company's own record of its people. The profile hangs off the
> MEMBERSHIP, not the user: `users` has no RLS by design (it is read before
> any tenant context exists), so an address stored there would be readable
> by every company that person also belongs to. The composite FK into
> `company_users` with ON DELETE CASCADE is what stops a profile outliving
> the membership it describes.

```mermaid
erDiagram
    professions {
        uuid id "PK"
        uuid company_id "FK"
        varchar name "unique per company, case-insensitive"
        timestamptz created_at
    }
    member_profiles {
        uuid id "PK"
        uuid company_id "PK,FK"
        uuid user_id "PK,FK"
        varchar first_name
        varchar last_name
        varchar address_line1
        varchar address_line2
        varchar city
        varchar state
        varchar postal_code
        text notes "the company's private record ABOUT them"
        uuid profession_id "FK, ON DELETE SET NULL"
        varchar image_path "relative to STORAGE_ROOT"
        timestamptz created_at
        timestamptz updated_at
    }
    member_phones {
        uuid id "PK"
        uuid company_id "FK"
        uuid member_profile_id "FK, ON DELETE CASCADE"
        varchar label "free text"
        varchar number "stored as typed"
        timestamptz created_at
    }
    companies ||--o{ professions : "defines"
    companies ||--o{ member_profiles : "scopes"
    company_users ||--|| member_profiles : "described by"
    professions |o--o{ member_profiles : "classifies"
    member_profiles ||--o{ member_phones : "reachable on"
```

## Outbound email and account recovery (migrations 0027–0029)

> Two tables and one column that decide how mail leaves and how somebody
> gets back in. `company_branding.email_sender_name` (0027) is the display
> name in front of the address; `company_email_settings` (0029) is a
> tenant's own SMTP server, with the password held as a Fernet ciphertext
> and returned by no route. `password_reset_tokens` (0028) is the only
> table here outside the tenant model — see the no-RLS note at the top.

```mermaid
erDiagram
    company_email_settings {
        uuid id "PK"
        uuid company_id "FK, unique"
        varchar host
        int port
        varchar username
        text password_encrypted "Fernet; never returned by a route"
        varchar from_address
        bool starttls
        bool enabled "false = fall back to the platform relay"
        timestamptz verified_at "null until a test message got through"
        timestamptz created_at
        timestamptz updated_at
    }
    password_reset_tokens {
        uuid id "PK"
        uuid user_id "FK, ON DELETE CASCADE"
        varchar token_hash "SHA-256; the secret lives in one email"
        timestamptz created_at
        timestamptz expires_at "one hour"
        timestamptz used_at "single use"
    }
    companies ||--o| company_email_settings : "sends through"
    users ||--o{ password_reset_tokens : "recovers with"
```

## Cross-domain foreign keys

Relationships that cross the groupings above, listed rather than drawn so the
per-domain diagrams stay readable.

| From | Column | References |
|---|---|---|
| `bill_payments` | `company_id` | `companies` |
| `bill_payments` | `recorded_by` | `users` |
| `bills` | `company_id` | `companies` |
| `bills` | `project_id` | `projects` |
| `bills` | `subcontractor_id` | `subcontractors` |
| `bom_line_receipts` | `company_id` | `companies` |
| `bom_line_receipts` | `recorded_by_user_id` | `users` |
| `bom_lines` | `company_id` | `companies` |
| `bom_lines` | `cost_catalog_item_id` | `cost_catalog_items` |
| `bom_lines` | `project_id` | `projects` |
| `change_orders` | `company_id` | `companies` |
| `change_orders` | `project_id` | `projects` |
| `communication_logs` | `author_id` | `users` |
| `communication_logs` | `company_id` | `companies` |
| `compliance_documents` | `company_id` | `companies` |
| `compliance_notifications` | `company_id` | `companies` |
| `company_financial_settings` | `company_id` | `companies` |
| `cost_catalog_items` | `company_id` | `companies` |
| `daily_logs` | `author_id` | `users` |
| `daily_logs` | `company_id` | `companies` |
| `documents` | `company_id` | `companies` |
| `documents` | `uploaded_by` | `users` |
| `esignatures` | `company_id` | `companies` |
| `esignatures` | `signed_by_user_id` | `users` |
| `estimate_line_items` | `company_id` | `companies` |
| `estimates` | `company_id` | `companies` |
| `estimates` | `lead_id` | `leads` |
| `estimates` | `project_id` | `projects` |
| `expenses` | `company_id` | `companies` |
| `expenses` | `project_id` | `projects` |
| `integration_connections` | `company_id` | `companies` |
| `integration_entity_mappings` | `company_id` | `companies` |
| `integration_sync_records` | `company_id` | `companies` |
| `invoice_payments` | `company_id` | `companies` |
| `invoice_payments` | `recorded_by` | `users` |
| `invoices` | `company_id` | `companies` |
| `invoices` | `estimate_id` | `estimates` |
| `invoices` | `project_id` | `projects` |
| `lead_clients` | `company_id` | `companies` |
| `lead_clients` | `user_id` | `users` |
| `leads` | `company_id` | `companies` |
| `markup_profiles` | `company_id` | `companies` |
| `phases` | `company_id` | `companies` |
| `project_clients` | `company_id` | `companies` |
| `project_clients` | `user_id` | `users` |
| `projects` | `company_id` | `companies` |
| `projects` | `lead_id` | `leads` |
| `subcontractor_assignments` | `assigned_by` | `users` |
| `subcontractor_assignments` | `company_id` | `companies` |
| `subcontractor_assignments` | `project_id` | `projects` |
| `subcontractors` | `company_id` | `companies` |
| `tasks` | `assignee_id` | `users` |
| `tasks` | `company_id` | `companies` |
| `vendors` | `company_id` | `companies` |

_54 cross-domain references._

