---
schema_version: 2
id: spec.eventing.transport.0001
type: spec
title: Event Transport for Widget Processing
status: accepted
source: agent
scope: platform
repos:
  - svc-widgets
domains:
  - eventing
created_at: 2026-07-28T00:00:00Z
updated_at: 2026-08-01T10:00:00Z
---
# Event Transport for Widget Processing

## Decision

Widget processing events are transported over **Amazon SQS standard queues**.
One queue per event class: `widgets-ingest`, `widgets-process`,
`widgets-publish`.

## Requirements

- Every queue has a dead-letter queue. Messages move to the DLQ after
  **5 receives** (`maxReceiveCount: 5`).
- Visibility timeout is **90 seconds** on all widget queues.
- Message bodies are JSON with a required `event_version` field, currently
  `"2"`.
- Consumers must be idempotent: SQS standard queues deliver at-least-once,
  and duplicate delivery is expected behavior, not an error.

## Alternatives considered

- **SNS fan-out**: rejected — no consumer needs broadcast semantics today.
- **Kinesis**: rejected — ordering is not required and shard management adds
  operational load with no benefit at current volume.

## Consequences

- Ordering is not guaranteed. Any consumer that needs ordering must sort on
  the event `occurred_at` field.
- Queue depth is the primary scaling signal for widget workers.
