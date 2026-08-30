---
schema_version: 2
id: adr.eventing.transport-eventbridge.0001
type: adr
title: Move Widget Event Transport to EventBridge
status: draft
source: agent
scope: platform
repos:
  - svc-widgets
domains:
  - eventing
created_at: 2026-08-20T00:00:00Z
updated_at: 2026-08-20T15:30:00Z
---
# Move Widget Event Transport to EventBridge

**Status: DRAFT — proposal only. The accepted transport remains SQS per
`spec.eventing.transport.0001`. Nothing in this document is decided.**

## Context

The SQS-per-event-class design (three queues, one consumer group each) is
showing friction: adding a new consumer to an existing event class requires
either queue fan-out plumbing or consumer-side filtering.

## Proposal

Replace the SQS standard queues with an **EventBridge event bus**
(`widgets-events`) using rule-based routing. Each consumer declares a rule
matching its event patterns; new consumers attach without touching
producers.

- Producers publish to the bus with `detail-type` set to the event class.
- Per-consumer SQS queues remain as rule targets, so consumer-side
  semantics (retry, DLQ) are unchanged.
- Proposed DLQ policy for rule targets: `maxReceiveCount: 3` (down from 5,
  rationale: EventBridge retries delivery to the target queue itself).

## Open questions

- Cost at current event volume vs. three flat queues.
- Whether `event_version` moves into `detail-type` or stays in the body.
- Migration sequencing: dual-publish window or hard cutover.

## Direction if accepted

This would supersede `spec.eventing.transport.0001`. It has not been
reviewed and does not represent current platform direction.
