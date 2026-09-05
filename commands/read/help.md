# read

Read the memory bank through the pack's read-only CLI adapter.
The supported v1 import binding is `hindsight`, including when a consuming
agent belongs to a different pack.

```bash
gc hindsight read memory reflect "$HINDSIGHT_BANK" "<task>" --budget mid
gc hindsight read -o json memory recall "$HINDSIGHT_BANK" "<question>" --budget low
gc hindsight read -o json mental-model get "$HINDSIGHT_BANK" landmines
```

Arguments pass unchanged to the existing adapter. It permits only
`memory recall/reflect`, `document get/list`, `mental-model get/list`,
`operation get/list`, `bank config/stats`, and `tag list`. Writes are rejected.
Output options such as `-o json` precede the operation.

The adapter shares shipping's endpoint and credential resolution, including
the `HINDSIGHT_API` alias. Supply the bank as the operation's positional
argument. Output and exit status pass through unchanged.
