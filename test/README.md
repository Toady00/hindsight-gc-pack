# Verifying the pack

Run the fast, in-process checks used by pre-push:

```bash
lefthook run pre-push
```

Run the complete offline suite, including shell, Git, and installed-gc checks:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s test -p 'test_*.py' -v
gc lint .
```

The suite uses standard-library `unittest` and `unittest.mock`. Keep validation
and failure permutations in-process; use shell tests for command composition,
not to repeat the ingestion state machine.

| Tests | Responsibility |
|---|---|
| `test_ingestion.py` | Recovery, receipts, lost acknowledgements, child extraction errors, Beads and HTTP contracts |
| In-process classes in `test_pack.py` | Schema, shipping orchestration, scan health, GONE boundaries, request validation |
| `MemoryCLITest` | Memory payload hashes, deliberate reports, recovery markers and errors |
| `test_git_snapshot.py` | Real Git publication branches, fetch failures, immutable snapshots, worktrees |
| `PackShellTest` and `MemoryShellTest` | Narrow shipping/recovery paths, queueing, audits, recovery before bumping hit counts |
| `test_commands.py` | Wrapper forwarding, admission guards, real Gas City rendering and dispatch |

All fixture repositories, service records and logs go into temporary directories.
No offline test contacts live services or makes LLM calls. Only the Git tests
and two shipping shell tests create repositories. The small shell-service fixture
supports those smoke tests, not a general Beads/Hindsight simulator. Detailed
recovery behavior belongs in `test_ingestion.py`.

Gas City compatibility checks use `gc prime --strict` and real command dispatch
against a temporary city, without registering or starting it. They skip explicitly
if `gc` is absent. No Go compiler or replica renderer is needed. To check a specific
Gas City binary:

```bash
GC_TEST_BIN=/path/to/gc python3 test/test_commands.py GasCityCompatibilityTest
```

Pre-push runs only the in-process classes listed in `lefthook.yml`; pre-commit
still runs `gc lint .`. Run full discovery before publishing changes to scripts,
Git traversal, commands, prompts, or formulas. The full suite remains explicit,
not a hidden background job or an installed CI workflow.

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
