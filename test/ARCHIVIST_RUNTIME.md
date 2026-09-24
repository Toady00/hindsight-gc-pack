# Archivist runtime regression

Run from the pack root:

```sh
HINDSIGHT_RUNTIME_TEST=1 python3 -B -m unittest test.test_archivist_runtime -v
```

Requires Python 3.11+, installed `gc 1.4.2+local.4eb766c0b`, `bd`, `dolt`,
`jq`, and Bash 4.4+ on PATH. `GC_TEST_BIN` can select the gc executable; the version check still applies.
Without the opt-in flag, unittest discovery skips these tests without starting
services. Each test creates a private city, HOME, GC_HOME, runtime directory,
Beads database, loopback Dolt server, and foreground gc supervisor under `/tmp`.
The short path avoids Darwin's Unix socket path limit. Cleanup stops that city,
supervisor, and Dolt server before deleting the temporary directory.

The test copies the pack without changing its formulas, agent configuration, or
command wrappers. It requires root-only vapor formulas with the numbered phase
headings and explicit root closure instructions. It does not convert legacy
`[[steps]]` formulas into a passing fixture.

## Coverage

- Two workflows queued together execute maintenance then shipping in the same
  named worker, with no external reminder or reset between claims. The fixture
  seeds a dormant named session record through Beads CLI to match an existing
  city; the controller alone starts its process. Both claims remain visible
  for two patrol intervals before command execution.
- A separate test explicitly resets that isolated named session after a real
  Ingestor success receipt has been stored in Beads. The next pass reports a
  synthetic bank-drain timeout. Real ScanReport/status code includes the earlier
  task-attributed receipt despite the latest scan's zero writes, and the Hindsight
  API stub records exactly one POST. This reset is fault injection, not a wake
  shortcut. The subprocess fixture records and cleans up surviving tool processes.

- Human `gc hindsight ship --reprocess ...` queues the real encoded request.
  The success case first registers the city and observes two idle patrol intervals
  with no demand or wake, then submits work to that same running controller. This
  verifies wake on newly arrived work, not only startup backlog.
- The real controller dispatches the unchanged shipping and maintenance cooldown
  orders in separate cities, retaining their original 1h and 24h intervals.
  There is no test-side `gc order run`, session start, or hook claim.
- The compiled workflow contains only its root. Both `gc ready` and the actual
  `bd ready` routed-work predicate must admit the instantiated root as a task.
  Real `bd list` queries verify one persisted formula root and no children both
  before claiming and after completion. Children can lack `gc.formula_name`, so
  a metadata-filtered count alone is insufficient; the test also queries `--parent`.
- The controller must log demand and wake a provider process. That process must
  claim through `gc hook --claim --json`, with ownership verified by `gc bd show`.
- The provider executes commands parsed from the claimed bead's actual phase
  descriptions, in order. A short barrier lets the parent observe Ready before
  the provider claims; it does not start or wake the provider.
- Real pack wrappers dispatch into mocked shipping/maintenance scripts and a
  mocked Hindsight CLI. Request arguments, including a path with spaces, survive.
- Maintenance exits 0 and 2 fetch both configured mental models. Exit 2 reports
  findings, not a clean run or incomplete maintenance. Inventory errors skip model
  reads. Shipping drain timeout and maintenance inventory error still produce
  non-clean reports and close the root.
- After successful maintenance, injected model-read failure and malformed JSON
  each produce an incomplete model audit, never a clean report. The provider
  stops at the first failed model, records its ID, command and diagnostic in notes,
  and closes the root with the diagnostic in an explicit incomplete reason.
  Maintenance's exit code remains 0; audit failure is recorded separately.
- Report notes must be persisted and read back while the root remains in progress,
  before the confirmed owner closes it without `--force`. The provider parses the
  description's model-audit exit-code gate, maintenance result branches, and final
  notes-before-close/incomplete-work instructions. It passes an explicit reason
  distinguishing clean completion, findings, and incomplete work, including the
  exit code. Both provider and parent verify the persisted close reason.
  Both the source and instantiated descriptions must explicitly instruct closure
  as the confirmed assignee using `--actor`, never `--force`, matching the fixture.
- Shipping descriptions retain the receipt inspection and no-blind-reprocess
  guidance. An idle controller observes two patrol intervals without any wake.
- A negative regression control instantiates and routes a temporary legacy vapor
  formula with `pour = true` and one executable child. It verifies the actual
  root and child exist, `bd ready` sees the child, and `gc ready` excludes both.
  Across two 5s patrol intervals there must be no positive demand, provider wake,
  claim, command execution, or closure. It does not reject `pour` before cooking.

## Boundaries

The provider is a deterministic Python program launched by a shell wrapper. It
does not test an LLM's ability to interpret instructions, judge model overreach,
decide escalation, or send incident mail. It synthesizes
the report from controlled command results, using real receipts in the reset test.
Full Git shipping, consolidation, extraction,
and Hindsight server behavior remain outside this test. No live city or Hindsight
credentials are inherited, and no Hindsight server is started.

The isolated PATH explicitly includes the installed modern Bash. With macOS's
`/bin/bash` 3.2, the unchanged shipping wrapper's scheduled empty-roots path fails
at `ROOTS[@]` under `set -u`, before reaching the mocked shipper. The harness
checks nounset-safe empty-array expansion up front; it does not modify the wrapper
or substitute a nonempty docs root to hide that shell requirement.

In a fresh fixture city, template-routed work starts an ephemeral demand worker.
The continuity tests additionally seed a dormant named identity and verify its
wake and sequential claims. The normal agent capacity and named-session
configuration are copied unchanged. These are deterministic-provider tests;
they do not prove that a particular LLM will consistently follow the hook loop.

The 5s patrol avoids an installed-runtime race exposed by 200ms polling, where
phantom pool identity collapse can reap a worker during its claim. These tests
cover individual workflows, a two-item queue, and an explicit mid-task reset,
not sustained operation or every failure/re-wake mode. They require real Beads because the installed hook
claim path uses BdStore even when other commands select the file store.
