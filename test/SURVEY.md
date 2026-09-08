# Live rig survey test

Start with `publish=none`. It exercises the real surveyor, Git checkout,
document generation, schema validation and signing without a push or a
Hindsight retain. It creates real work and a local commit, not a read-only
dry run. Offline coverage lives in `test_survey.py` and
`test_survey_integration.py`.

## Prerequisites

- Use the `hindsight` import binding. Keep the existing city import for the
  archivist. Add a rig import under the selected rig's existing `[[rigs]]`
  entry; do not create a duplicate rig entry:

```toml
[[rigs]]
name = "example"
path = "/absolute/path/to/rig"

[rigs.imports.hindsight]
source = "/absolute/path/to/hindsight"
```

- The city/controller must be running and the selected rig must be enabled
  for work. No existing survey should be active for this rig. Formula cooking
  does not provide the scheduled order's duplicate-run suppression.
- The rig must be a Git checkout with an accessible `origin` and a resolvable
  remote default branch. Preparation fetches that branch, not local edits.
- The surveyor process needs `gc`, `git`, `bash`, `jq`, Mike Farah's `yq`,
  Python 3, and access to the configured Git signing key. A signing failure
  must stop the run; do not bypass it.
- PR tests additionally need authenticated `gh` or `glab` and push access.
  Self-hosted hosts that cannot be inferred need `--var pr_tool=gh` or
  `--var pr_tool=glab` when cooking the formula.

From the city directory, check discovery and compilation before creating work:

```bash
gc agent list
gc order list
gc formula show current-state-survey --rig <rig> --json
```

Confirm `<rig>/hindsight.surveyor`, the rig's `current-state-survey` order,
and resolved descriptions for prepare, survey, publish and cleanup. A city
import alone does not create the rig order.

## Start the local-only run

From the city directory:

```bash
gc formula cook current-state-survey --rig <rig> --var publish=none --json
```

Record the returned workflow root ID as `<root>`. Current integration tests
verify overrides through both `formula cook` and `order run --var`. Older
`gc` builds silently used defaults for order overrides; check persisted
instructions before relying on local-only publication.

Inspect the actual step instructions, not just the command's exit code:

```bash
gc bd --rig <rig> list --parent <root> --status all --limit 0 --json
```

The prepare description must contain `--publish "none"`. Its routing must
target `<rig>/hindsight.surveyor`. The provider should wake, claim the prepare
step, and proceed through survey and publish. Scope checks and finalization
are controller work; do not close them manually to make the run look passed.

Once preparation has run, from inside the rig checkout:

```bash
gc hindsight survey show <root>
```

Verify `survey_publish=none`, the recorded immutable `survey_base`, and a
`work_dir` below `<city>/.gc/worktrees/<rig>/current-state-<root>`. The branch
must be `survey/current-state-<root>`, not the rig's default branch.

## Acceptance checks

- The surveyor actually wakes and claims routed work without a manual prompt.
- Prepare, survey and publish close with `gc.outcome=pass`; the worktree scope
  body closes with pass, and cleanup records preservation for `publish=none`.
  Root finalization and cleanup may finish in either order, so inspect both.
- New runs default to `<work_dir>/docs/current-state.md`. With a custom
  `output_file`, check that full file path instead. Active runs keep the
  `survey_doc` recorded on their root, including `docs/current-state/README.md`.
  Its frontmatter contains `id: current-state.<rig>`, `type: current-state`,
  `status: draft`, and `source: agent`.
- The content cites real code paths/lines, explains architecture and entry
  points, and distinguishes verified behavior from untested claims. Existing
  README text must not substitute for reading code.
- The worktree is clean and the commit is signed. Inspect without changing it:

```bash
git -C <work_dir> status --short
git -C <work_dir> log -1 --show-signature
git -C <work_dir> diff --stat <survey_base> HEAD
```

- Only the survey document changed relative to `survey_base`. The original
  rig checkout and origin branches were not changed by this local-only run.
- No PR/MR was opened and no bank write occurred. The survey command itself
  never calls Hindsight; ingestion only sees publication to the configured
  canonical branch.

## Publication test

After inspecting the local-only run, start a new root for PR publication:

```bash
gc formula cook current-state-survey --rig <rig> --var publish=pr --json
```

Publication mode is immutable for a root. Do not edit the old root's metadata
to turn a `none` run into a publishing run. A new root has a separate branch
and worktree. The default monthly order also uses PR mode.

Confirm the PR/MR targets the correct default branch and changes only the
survey document. Successful cleanup requires a closed passing scope, a clean
worktree, a publication receipt and the commit on origin. Remote/forge errors
must fail the publish step and preserve local work.

Review approval is separate from authoring: all modes generate agent-authored
drafts, and merging does not promote their status automatically. If a reviewer
accepts the record, update its status deliberately. It remains observed code
state, not an adopted design decision. Once merged into the configured docs
tree, normal Git ingestion ships it, including if it remains a draft.

## Recovery

Failed steps must report failure rather than close with caveats. Inspect the
step notes and `gc hindsight survey show <root>` from the rig checkout.
Retrying preparation preserves the recorded base, branch, commits and dirty
work. Conflicting options, foreign worktrees and old handoffs lacking ownership
metadata are refused. Direct publication never rebases or force-pushes after
rejection; reconcile manually or use a new run.

Keep a failed or local-only worktree until its contents have been reviewed.
Normal cleanup is safe to retry, including after interrupted removal. To
discard a preserved run deliberately, from outside its worktree and inside
the rig checkout:

```bash
gc hindsight survey cleanup <root> --force
```

This deletes that run's local branch and worktree, including uncommitted or
ignored files. It does not delete a remote branch or a PR. Do not use it while
the surveyor is still working in the run.
