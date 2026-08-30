---
schema_version: 2
id: convention.platform.database.0001
type: convention
title: Platform Database Convention
status: accepted
source: human
scope: platform
domains:
  - storage
created_at: 2026-07-20T00:00:00Z
updated_at: 2026-07-20T12:00:00Z
---
# Platform Database Convention

## Convention

The platform database is **PostgreSQL 16**. Ranked preference:

1. **PostgreSQL 16** — every service datastore, no exceptions without an
   ADR.
2. **SQLite** — permitted only for embedded tooling and local development
   fixtures, never as a service datastore.

**Anti-preference: MySQL is not used on this platform.** The bar to
override: a vendor dependency that cannot run against Postgres, recorded in
an accepted ADR.

## Canonical setup

- Provisioned via the platform Terraform modules (`module "postgres"`),
  one instance per service, no shared databases between services.
- Migrations run with `sqitch`; migration files live in the owning
  service's repo under `migrations/`.
- Connection pooling through `pgbouncer` in transaction mode.

## Applies to

All service datastores, platform-wide. Analytics workloads have no decided
exception as of this convention's date — any analytics-specific store
requires its own accepted ADR.
