# check

Read-only validation of one repository's publishable documents. The publisher
runs exactly these checks on every scan before it writes anything; running them
earlier only moves the same failures closer to the edit.

```bash
gc hindsight check                       # docs/ in the current repository's working tree
gc hindsight check docs/initiatives/foo  # selected files or directories
gc hindsight check --rev HEAD            # a commit instead of the working tree
gc hindsight check --fingerprint docs/initiatives/foo
```

From an external or sibling checkout, supply city context with `GC_CITY`, or run
`commands/check/run.sh` from the Hindsight pack directly. It needs `python3`,
`git` and Mike Farah `yq`; it needs no bank, Beads, credentials or archivist.

## What it checks

- Every shippable document passes the schema (`schemas/docs/derive`), including
  duplicate frontmatter fields, which are refused rather than resolved silently.
- Discussion records omit `status` entirely. Other types require it. Discussions
  preserve conversation history, not approved scope, and cannot be report pins.
- `.hindsight-namespace` at the repository root holds one key: the city or rig
  name, lowercased, using letters, digits, `_` or `-` (never `rig` or `city`).
- Each document ID is `<namespace>.<document-id>`, with no literal `rig` or
  `city` component after the namespace. The `.0001`-style suffix is part of a
  stable identity, not a revision counter.
- IDs are unique in the repository, counting retired (superseded/deprecated)
  documents, which keep their IDs forever.
- Build reports pin non-discussion document revisions in the same namespace.
  Each ID/fingerprint pair must match a valid current document or a historical
  Markdown blob reachable from the selected commit, including moved or deleted
  documents. A report need not assess the latest files or bank-visible revisions.
  Unknown fingerprints are errors, not permission to rewrite a report's pins.
  Historical lookup covers Markdown throughout that repository's ancestry,
  including drafts outside today's docs roots. It verifies recorded identity,
  type and exact fingerprint, without applying today's required fields to old
  records. This proves existence, not approval, implementation or code-SHA validity.
  Working-tree-only matches can pass locally but must be committed and published
  before shipping. Shallow history must be fetched when an old pin is unavailable.

`--fingerprint` prints `<fingerprint>  <id>  <path>` for shippable documents. The
fingerprint hashes the file with only the values of `status` and `updated_at`
masked. Discussions omit status, so only `updated_at` is masked for them.
Build reports pin non-discussion documents in `assesses`.

Exit codes: `0` clean (warnings allowed), `1` contract violations, each printed
with repair guidance, `2` invalid invocation or missing tools.

## What it cannot check

Publication history lives in the city's publication records, so only the
publisher enforces: drafts after first acceptance staying unpublished, frozen
superseded/deprecated content, status-only changes against the last published
content and namespace ownership across repositories. Reports and intent publish independently.
`gc hindsight ship --dry-run` previews those decisions.

## Optional wiring

The pack installs no hooks and edits no hook configuration. To run the check
yourself, for example with Lefthook:

```yaml
pre-push:
  commands:
    hindsight-docs:
      run: gc hindsight check --rev HEAD
```

or in CI, from a checkout with the pack available:

```bash
/path/to/hindsight/commands/check/run.sh --rev "$GITHUB_SHA"
```

The check reads the working tree or a commit, never the Git index, so it does not
check staged content separately. Prefer `--rev HEAD` in pre-push hooks.
