# maintain

Run deterministic bank maintenance in the managed archivist. The supported
v1 import binding is `hindsight`.

```bash
gc hindsight maintain
gc hindsight maintain --bank <id> --drain-timeout 600 --consolidate-timeout 900
gc hindsight maintain --skip-consolidate
```

Arguments pass unchanged to `bank-maintain.sh`, which owns writer admission,
draining, consolidation, and audits. It also accepts `--api`, `--domains`,
and `--schema`. The default bank is `$HINDSIGHT_BANK`.

Consolidation uses JSON submission and polls the returned operation ID directly,
keeping failure details visible without animated progress. Each operation has a
900-second wait limit, configurable with `--consolidate-timeout`. Confirmed
terminal failures get one recover-and-retry attempt. A timeout, unreadable
status, or uncertain submission is not automatically retried.

This command executes in the foreground, not through a queue. Only the managed
archivist may consolidate. The existing `--skip-consolidate` mode permits
read-only drain and audit outside the writer session. Never copy the writer
marker to another session. Run ship, retain, and maintenance sequentially.

Output and exit status pass through unchanged: 0 is clean, 2 reports audit
findings or denied writer admission, 3 is a drain timeout, 4 is failed
consolidation after retry or a consolidation timeout, and 5 is unavailable
operation or audit state, including an uncertain consolidation submission.
Invalid arguments return 64; other downstream failures may also be nonzero.
