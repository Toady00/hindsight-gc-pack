# survey

The mechanical steps of a rig's `current-state` survey, exposed as one
command so the surveyor agent's formula steps and a human at the shell run
exactly the same code. The agent reads the repository and writes the
document; everything else — the worktree, the frontmatter, the push/PR,
the cleanup — is this command.

## Usage

```
survey prepare <root> [--output-dir <dir>] [--publish pr|direct|none] [--pr-tool auto|gh|glab]
survey show    <root>
survey stamp   <root>
survey publish <root> [--body-file <file>]
survey cleanup <root> [--force]
```

`<root>` is the workflow root bead of one `current-state-survey` run; all
handoff state lives in its metadata (`survey show` prints it).

- `prepare` — fetch the rig's remote default branch, create a worktree at
  `<city>/.gc/worktrees/<rig>/current-state-<root>` on branch
  `survey/current-state-<yyyymmdd>`, and record `work_dir` and the run's
  options on the root. Defaults: `docs/current-state`, `pr`, `auto`.
- `stamp` — write the `current-state` frontmatter (`id:
  current-state.<rig>`, `source:` `human` for `pr` runs, `agent`
  otherwise, `updated_at:` now) over the document, replacing any existing
  block, and validate it against `schemas/docs/derive`. Refuses without
  touching the file if the schema refuses.
- `publish` — `pr`: push the branch and open a PR (`gh`) or MR (`glab`),
  reusing an open one; the tool is inferred from the `origin` host unless
  `--pr-tool` was given. `direct`: push the commit to the default branch,
  rebasing once on rejection. `none`: record and stop.
- `cleanup` — remove the worktree and local branch only when the run
  passed, something was published, and the worktree's HEAD is on origin.
  Anything else is preserved with the reason stamped on the root;
  `--force` removes regardless.

## When to reach for it

- Running a survey by hand: `gc order run current-state-survey --rig
  <rig>` fires the whole workflow; the subcommands are what its steps call.
- A run was preserved (failed, or `--publish none`): inspect the worktree
  `survey show <root>` names, then `survey cleanup <root> --force`.
- Re-stamping a hand-edited survey before committing: `survey stamp <root>`.

Identity comes from the managed session env (`GC_RIG`, `GC_RIG_ROOT`,
`GC_CITY`); from a shell, run it inside the rig checkout and it resolves
them from `gc rig list`.
