---
schema_version: 2
id: discussion.memory.semantic-checks.0001
type: discussion
title: Optional semantic document checks and follow-on experiments
status: draft
source: agent
scope: repo
repos: [hindsight]
domains: [memory]
created_at: 2026-09-23T00:00:00Z
updated_at: 2026-09-23T00:00:00Z
---
# Optional semantic document checks

This draft records the September 23 discussion about TypeSafe Jev experiments.
The requested first experiment is a standalone `gc hindsight lint` command.
The other ideas below are proposals, not adopted behavior or scheduled work.
Jev access is limited and invite-only at the time of this discussion. The pack
must remain usable without it.

## Design boundary

Use Jev for bounded semantic judgments over readily available state. Code owns
parsing, exact validation, provenance, side effects, and execution. Jev returns
choices, yes probabilities, or scores; it does not retrieve evidence or generate
reviews. If an agent must investigate the corpus to prepare the state, it may
already have done most of the work. That is a poor starting point for this PoC.

Keep state focused without generating summaries that might discard qualifications.
Start with one complete document and its applicable checks. The initial local byte
caps rejected an ordinary project ADR and were removed; the API enforces the
selected model's token limits. Byte counts in previews are not token counts.
Test section-based approaches separately. An uncertain response and an unavailable
service are distinct from a clean result. Model probabilities need validation on
this pack's documents.

## Experiment 1: semantic document linting

Status: standalone PoC, opt-in. See [command usage](../commands/lint/help.md).

Input: explicit local docs-schema Markdown files, parsed frontmatter and original
body. No Hindsight lookup or reasoning-model preprocessing. The command validates
the existing schema, then asks independent applicable questions about accepted
versus draft standing, repo versus platform authority, surveys that are primarily
future plans, and gotchas that are only transient incidents.

Output: per-rule probabilities and finding/uncertain/clear status, source hash,
request state, model/rule versions, usage and latency. Dry-run works without an API
key. Reports suggest investigation; they do not update frontmatter or bank tags.

Evaluate labeled examples, including hard negatives such as historical draft
notes, quotations, other documents' statuses, and recommendations within observed
surveys. Record false positives, missed findings, uncertain cases, cost, and
latency. A wiring decision requires useful performance on representative real
documents, rather than a successful mock test or a few convincing examples.

## Proposal 2: frontmatter suggestions and consistency checks

Input: document body, existing frontmatter, authoritative schema vocabularies,
rig registry and short repo descriptions, and configured domain vocabulary.
Existing bank tags may suggest candidates, but must not define validity: the bank
can contain malformed tags from bypassed writers.

Ask for a document-type suggestion and independent applicability judgments for
each domain or repository candidate. Allow no match. Flag scope inconsistencies.
Keep source identity, approval provenance, IDs and date validation deterministic.
Prose alone cannot establish acceptance or human authorship.

Begin with suggested edits for authors. Any later automatic update must happen in
the source Git document and preserve the current schema-derived tag/strategy
contract. Evaluate against author-labeled documents, ambiguous multi-domain docs,
and cases where the correct value is absent from the candidates.

Revisit after the linting PoC demonstrates useful judgments on the same corpus.
A bounded evaluation fits a `spike` bead; a chosen implementation fits `feature`.

## Proposal 3: memory-proposal intake checks

Input: the proposal mail workers send the archivist after a verified, surprising
platform failure. The mail supplies what happened, how it was verified, how the
failure presented, and what future agents should do differently.

Possible checks: missing required information, platform versus city-operations
scope, whether the described failure is immediate and self-explanatory, and
whether it only describes temporary work state. Return structured intake findings
before the archivist spends effort investigating.

Keep the archivist's existing arbitration policy. A persuasive mail is not proof
of its claims. Verification and deduplication need evidence beyond the mail.
Later, if recall supplies good candidates cheaply, test whether Jev can distinguish
same finding, added information, contradiction, and unrelated content. That could
assist the existing new-memory versus `--bump` paths; merging content still needs
an author or generative model. All four acceptance gates remain independent.

Evaluate against labeled proposals, especially convincing but unverified reports,
useful city-operations knowledge that belongs outside the bank, and related errors
with different causes. Start with a `spike` when sufficient examples exist.

## Proposal 4: select candidates before expensive reasoning

Input: a task and already-retrieved candidate memories or document passages.
Score direct task relevance, constraints and applicable traps. Code selects a
bounded context set while preserving conflicting evidence and source standing.
This is selection before synthesis, not trimming an already-reasoned reflect
response. Hindsight already selects context internally; an extra stage requires
a demonstrated retrieval failure and comparison with the current baseline.

Repeated gotcha reports may provide evaluation cases. Combine semantic matching
with mechanically recorded retrieval and brief inclusion to distinguish memories
never retrieved, retrieved but omitted, and delivered but not used. Do not assume
hit count alone diagnoses the failure.

Revisit when concrete retrieval misses and usable candidates are available.

## Deferred ideas requiring better evidence plumbing

Mental-model audits, extraction verification, and intent-versus-implementation
drift detection may be useful if exact claim/evidence pairs are already available.
They are not early priorities if a reasoning agent must reconstruct those pairs.
Summarizing evidence can hide the very qualification under audit. Failed retrieval
does not prove a claim false.

Survey citation checks could mechanically load cited source at the surveyed
revision. Limit any experiment to claims the snippet can establish; architectural
claims often need call-chain or runtime investigation. Change-driven survey
targeting could compare Git changes with existing survey sections, but needs a
baseline showing it catches meaningful changes without losing coverage.

## Recording decisions and work

This document preserves rationale and alternatives without creating runnable work.
The installed Beads CLI lists `spike`, `feature`, and `decision` as core types.
Use `spike` for a chosen evaluation with explicit questions and exit criteria,
`feature` for implementation, and `decision` for a specific unresolved choice.
Link such beads to this document and section. Issue type alone does not keep a
bead out of ready work; set lifecycle/routing deliberately when creating one.
No proposal beads are created by the PoC.
