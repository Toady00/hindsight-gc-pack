Infrastructure only: create this run's isolated worktree. Do not read or
edit anything in the rig root checkout.

1. Read this step bead's metadata and take `gc.root_bead_id` as `<root>`;
   hard-fail if it is missing.
2. Run the pack's survey command:

       gc hindsight survey prepare <root> --output-dir "{{output_dir}}" --publish "{{publish}}" --pr-tool "{{pr_tool}}"

   (`hindsight` is the pack's import binding; use the city's binding name
   if it differs.) It fetches the remote default branch, creates the
   worktree and branch, records `work_dir` and the run's options on
   `<root>`, and prints them as `key=value` lines.
3. Copy the printed lines into this bead's notes. Verify with
   `gc bd --rig "$GC_RIG" show <root> --json` that `work_dir` is set and is not the rig
   root.

Close with `gc.outcome=pass`. If the command exits non-zero, close with
`gc.outcome=fail` and its stderr in the notes — do not try to create the
worktree by hand.
