---
schema_version: 2
id: spec.credentials.0001
type: spec
title: Credential handling requirements
status: accepted
source: human
scope: repo
repos:
  - svc-credentials
updated_at: 2026-08-03T12:00:00Z
---
# Credential handling requirements

Retrieve the FRED credential from SSM SecureString with decryption at runtime.
Do not persist the secret in Terraform state, environment variables, or logs.

For external dependencies, prefer reliable provider discovery. When it is
unavailable or unreliable, prefer the shared terraform-data-only interface.
Use an explicit configured identifier only if neither approach is suitable.
