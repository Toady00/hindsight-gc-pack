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
  `survey/current-state-<root>`. Persist the base commit, ownership and
  options before creation. Retries preserve existing commits and dirty work;
  conflicting options are refused. Defaults: `docs/current-state`, `pr`, `auto`.
- `stamp` — write the `current-state` frontmatter (`id:
  current-state.<rig>`, `status: draft`, `source: agent`, `updated_at:` now)
  over the document, replacing any existing
  block, and validate it against `schemas/docs/derive`. Refuses without
  touching the file if the schema refuses. Commit the stamped document with
  `git commit -S --only ... -- <document>` before publishing.
- `publish` — `pr`: push the branch and open a PR (`gh`) or MR (`glab`),
  reusing an open one; the tool is inferred from the `origin` host unless
  `--pr-tool` was given. `direct`: push the commit to the default branch,
  refusing on rejection without rebasing. `none`: record and stop. Every mode
  validates the committed draft and refuses commits touching other files.
  No force-pushes. Review approval is separate from publication.
- `cleanup` — remove the worktree and local branch only when the run
  has one closed passing worktree scope body, something was published,
  the worktree has no dirty or ignored files, and its HEAD is on origin.
  Anything else is preserved with the reason stamped on the root;
  `--force` explicitly discards the owned worktree, including uncommitted work.
  Run cleanup from outside the worktree. Interrupted removal can be retried.

## When to reach for it

- Running a survey by hand: `gc order run current-state-survey --rig
  <rig>` fires the whole workflow with PR publication defaults.
- First live test without publication: `gc formula cook current-state-survey
  --rig <rig> --var publish=none --json`. This creates real work, not a read-only
  dry run. Do not start another survey while one is active. The installed
  `order run --var` silently loses overrides, so use `formula cook` for options.
- A run was preserved (failed, or `--publish none`): inspect the worktree
  `gc hindsight survey show <root>` names. Keep it until review is complete;
  `gc hindsight survey cleanup <root> --force` discards it explicitly.
- Re-stamping a hand-edited survey before committing: `survey stamp <root>`.

Identity comes from the managed session env (`GC_RIG`, `GC_RIG_ROOT`,
`GC_CITY`); from a shell, run it inside the rig checkout and it resolves
them from `gc rig list`. Commands use the rig's Beads store explicitly.
The root must be a rig-owned `current-state-survey` workflow. Old handoffs
without ownership/base metadata are refused rather than reset or adopted.
See `test/SURVEY.md` in the pack for the live acceptance checklist.
