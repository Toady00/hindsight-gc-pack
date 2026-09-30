# read

Read the memory bank through the pack's read-only CLI adapter.
The supported v1 import binding is `hindsight`, including when a consuming
agent belongs to a different pack.

```bash
gc hindsight read -o json memory reflect "$HINDSIGHT_BANK" "<task>" --budget mid
gc hindsight read -o json memory recall "$HINDSIGHT_BANK" "<question>" --budget low
gc hindsight read -o json mental-model get "$HINDSIGHT_BANK" landmines
```

The adapter permits only
`memory recall/reflect`, `document get/list`, `mental-model get/list`,
`operation get/list`, `bank config/stats`, and `tag list`. Writes are rejected.
Output options such as `-o json` precede the operation.
When stdout is captured or piped, output defaults to JSON to avoid the CLI's
animated progress messages. An interactive terminal keeps the CLI's pretty
default. Explicit `-o` / `--output` choices take precedence; `-o pretty` can
produce repeated progress lines in captured output on older CLIs.

Agent instructions should request JSON explicitly, even when the adapter
would default to it. Each command joined with `&&` needs its own `-o json`.
For reflect, extract `.text` after a successful read:

```bash
TMP=$(mktemp -d)
gc hindsight read -o json memory reflect "$HINDSIGHT_BANK" "<task>" --budget mid > "$TMP/reflect.json" &&
  jq -er '.text' "$TMP/reflect.json"
```

Keep stderr visible. A failed read is not an empty memory result.

The adapter shares shipping's endpoint and credential resolution, including
the `HINDSIGHT_API` alias. Supply the bank as the operation's positional
argument. Apart from the captured-output default, arguments, output, errors,
and exit status pass through unchanged.
