Teardown runs after the worktree scope has settled. Read `gc.root_bead_id`
from this bead as `<root>` and run:

    gc hindsight survey cleanup <root>

It decides on its own: the worktree and local branch are removed only when
the worktree scope-body bead is closed with `gc.outcome=pass`, something was published, the worktree is
clean, and its `HEAD` is on origin. Otherwise it preserves the worktree and
stamps `survey_cleanup=preserved: <reason>` on `<root>` for an operator.
Never pass `--force` from this step, and never remove a worktree by hand.

The scope body has `gc.kind=scope`, `gc.scope_role=body`, and
`gc.step_ref=current-state-survey.worktree` under this exact root. The
root's `gc.outcome` is not the gate: workflow finalization can race cleanup.
Missing or ambiguous scope state must preserve the worktree.

Run cleanup from outside `work_dir`, for example `cd "$GC_CITY"`, so the
shell does not remain inside a directory the command removes.

Copy its output into the notes. Stamp this step's `gc.outcome=pass` on a
zero exit, or `gc.outcome=fail` on a non-zero exit, then close it. Preserving
a worktree is a successful cleanup decision, not a failed command. Never
change the root or scope-body outcome to make cleanup pass.
