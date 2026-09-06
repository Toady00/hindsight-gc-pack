Read `gc.root_bead_id` from this bead as `<root>`. Publishing is entirely
the pack's survey command; do not `git push`, open a PR, or touch the
default branch yourself.

1. Optional, `pr` mode only: write a short reviewer summary to a temp
   file — what the survey found notable, and what changed against the
   previous survey (`git diff origin/<survey_default_branch> --
   <survey_doc>` inside `work_dir`, from `gc hindsight survey show <root>`).
   Three to eight lines.
2. Run:

       gc hindsight survey publish <root> [--body-file <summary-file>]

   It reads the run's `survey_publish` mode from `<root>`: `pr` pushes the
   survey branch and opens (or reuses) a PR/MR, printing its URL; `direct`
   pushes to the default branch; `none` records that nothing was
   published.
3. Record the printed URL or status in this bead's notes.

Close with `gc.outcome=pass`. A non-zero exit is `gc.outcome=fail` with
stderr in the notes.
