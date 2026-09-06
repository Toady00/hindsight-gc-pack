Teardown; runs after the workflow has settled. Read `gc.root_bead_id`
from this bead as `<root>` and run:

    gc hindsight survey cleanup <root>

It decides on its own: the worktree and local branch are removed only when
the run's `gc.outcome` is `pass`, something was published, the worktree is
clean, and its `HEAD` is on origin. Otherwise it preserves the worktree and
stamps `survey_cleanup=preserved: <reason>` on `<root>` for an operator.
Never pass `--force` from this step, and never remove a worktree by hand.

Copy its output into the notes and close this bead. Its outcome does not
change the run's result.
