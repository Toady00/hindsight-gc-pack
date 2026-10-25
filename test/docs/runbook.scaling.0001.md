---
schema_version: 2
id: runbook.scaling.0001
type: runbook
title: Sandbox scaling procedure
status: accepted
source: human
scope: repo
repos:
  - svc-runbook
updated_at: 2026-08-02T12:00:00Z
---
# Sandbox scaling procedure

For the sandbox Deployment `runbook-worker` in namespace `sandbox`, the recorded
procedure for requesting three replicas is:

```bash
kubectl scale deployment/runbook-worker --replicas=3 --namespace=sandbox
```

This procedure is restricted to the sandbox. It neither authorizes production
changes nor records that anyone executed it.
