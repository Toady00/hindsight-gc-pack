# ship

Reconcile the platform's docs trees into the Hindsight memory bank —
manually, at any point. This is the same single ship path the archivist's
hourly order uses; running it by hand is always safe (idempotent, per-doc
serialized, refuses malformed frontmatter loudly).

## Usage

```
ship                    Auto-resolve roots (every rig's docs/ + city docs/), ship changes
ship --dry-run          Validate and report what would ship; writes nothing
ship <root> [<root>..]  Ship specific docs roots only
ship --bank <id> ...    Override the bank (e.g. a test bank rehearsal);
                        default is $HINDSIGHT_BANK from workspace env,
                        and there is no fallback — unset means refuse
ship --fetch            git fetch origin in each root first (the scheduled
                        order does this; ad-hoc runs usually don't need it)
```

Also honors `--api`, `--ref`, `--domains`, `--schema`, `--drain-timeout`
(see `assets/scripts/ship-docs.sh` header) and `HINDSIGHT_BANK` /
`HINDSIGHT_API` / `HINDSIGHT_SCHEMA` environment variables. The schema —
which frontmatter dialect the walked docs speak — defaults to the pack's
`schemas/docs`; one schema per run, per-city by design.

The shipper is **stateless**: the bank itself is the ledger (each retain
stamps a content hash the differ reads back), so this command computes the
same answer on any machine with no state file to sync or lose.

It ships **committed content from each root's canonical ref** (`--ref`
override → `origin/HEAD` → local `HEAD`), never the working tree.
Committing to that ref is the act of publishing; uncommitted and untracked
docs are unpublished and invisible. Non-git roots fall back to the
filesystem.

## When to reach for it

- You just hand-edited a doc and want it in the bank before the hourly
  order fires.
- You want to preview what a frontmatter change does: `ship --dry-run`.
- You are rehearsing a bulk load against a test bank: `ship --bank
  <bank>-test <root>`.

Exit non-zero means docs were refused (frontmatter violates the retain
contract) or failed extraction — read the per-doc report; fix the doc,
never work around the validator.
