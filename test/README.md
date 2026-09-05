# Verifying the pack

Run the offline production-path tests:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s test -p 'test_*.py' -v
gc lint .
```

All repositories, mock Beads records, subprocess logs and reports go into
temporary directories. Git tests use local bare origins and temporary clones or
worktrees. Most tests intercept gc, Hindsight and curl. A dispatch test also
uses the installed gc with a temporary city config and mocked Hindsight, without
registering or starting the city. No test contacts live services. Go is needed
to render the real prompt templates. Tests cover queue routing, endpoint and credential consistency, schema
validation, durable pinned records, intent-before-POST, failed retains that
already stamped bank hashes, original-operation recovery, and machine handoffs
through shared Beads. It also covers mandatory fetch, published branch selection,
dirty/unpushed content exclusion, traversal failures, unchanged/revised docs,
fail-closed reprocess acknowledgement loss, partial-root GONE, full-scan freshness,
unresolved attempts, paginated audits and memory bumps.

The historical `results/` files remain evidence of the original experiment.
They are not golden answers: the conventions model includes an unsupported
reaffirmation date. The new live checks treat that as a failure.

## Optional live evaluation

This consumes extraction and reflect calls. Run provisioning and shipping in
the managed archivist, sequentially, against a fresh disposable bank. Configure
`HINDSIGHT_API_URL` and `HINDSIGHT_API_KEY` for the intended server first. Do not
reuse a production bank or a bank containing additional documents. Set absolute
`GC_CITY_PATH` to the city configured for the shared Beads SQL database. The live
shipper writes pinned `hindsight-document` receipts and a `hindsight-bank` health
record there, keyed by endpoint and bank. The server's `/openapi.json` must
advertise `RetainRequest.operation_id` and boolean `async`; the local v0.9.1/0.9.2
servers used for the templates support this contract.

```bash
EVAL_BANK="hindsight-eval-$(date -u +%Y%m%dT%H%M%SZ)"
hindsight bank create "$EVAL_BANK"
hindsight bank import-template "$EVAL_BANK" example.bank-template.json
test/ship.sh "$EVAL_BANK"
assets/scripts/bank-maintain.sh --bank "$EVAL_BANK"
python3 test/evaluate.py "$EVAL_BANK"
```

`test/ship.sh` copies the current fixture sources into a temporary Git clone,
commits them and pushes to its local bare origin's `main` branch. The production
shipper then freshly fetches that origin, validates and polls operations through
confirmed completion. These are fixture-only Git commits, unsigned by default;
set `HINDSIGHT_TEST_COMMIT_GPGSIGN=true` to use the configured signing key. No
project commits or Git configuration are changed. The EXIT trap removes only
the generated temporary root, including its clone and bare origin.

The script requires the `hindsight-eval-` bank prefix and the managed archivist
writer guard. It writes to a live bank only when explicitly invoked; the offline
suite does not invoke live evaluation. Run it only against a fresh disposable
bank. Its explicit docs root is a partial scan, so it does not establish healthy
full-scan freshness. `gc hindsight status --bank "$EVAL_BANK"` reads shared Beads
without querying the bank. There is no `HINDSIGHT_STATE_DIR` fallback when city
state is unavailable.

Pending request payloads remain in Beads until successful completion, then leave
current metadata; Dolt history retains earlier source content. The EXIT trap
does not remove Beads records or bank data. A missing receipt never trusts a bank
hash: review a dry-run before a real bootstrap scan, which re-ingests once to
establish success. An unknown reprocess acknowledgement requires a human to
inspect operations and repair `metadata.hindsight.data.attempt` with the known
`reprocess_operation_id` and phase `reprocess_wait`; never blindly retry it. See
[OPERATIONS.md](../OPERATIONS.md) for the recovery procedure.

`evaluate.py` uses the production read adapter. It asks structured questions
about drafts, owner thinking, supersession, scope, exact numeric requirements,
and the distinction between a specification and verified implementation. No
accepted-source filter is added. Each run saves the full responses and a
machine-readable pass/fail report under a temporary directory, or `--output`.
These are regression probes, not a proof of answer quality across arbitrary
questions. The bank is left in place for inspection; deletion is manual.
