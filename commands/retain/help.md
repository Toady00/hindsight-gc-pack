# retain

Retain an arbitrated agent memory in the managed archivist. The supported
v1 import binding is `hindsight`. Other agents send memory proposals by mail;
this command is not an alternative to arbitration.

```bash
gc hindsight retain --id gotcha.example --title "Example" --repos <rig> < content.md
gc hindsight retain --bump --id gotcha.example
gc hindsight retain --bump --id gotcha.example --content-file merged.md
```

Arguments and stdin pass unchanged to `memory-retain.sh`. That helper owns
writer admission, contract validation, pending-operation checks, and polling.
The default bank is `$HINDSIGHT_BANK`. Its existing `--dry-run` mode previews
a payload without writing and does not require writer admission.

This command executes in the foreground, not through a queue. Never overlap
ship, retain, or maintenance, or copy the writer marker to another session.
Output and exit status pass through unchanged. A nonzero result is a failure,
not permission to bypass the helper with a direct API call.
