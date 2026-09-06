Read `gc.root_bead_id` from this bead as `<root>`, then
`gc hindsight survey show <root>` for the handoff. `cd` into `work_dir`
and confirm `pwd -P` matches before reading anything. Everything below
happens inside that worktree; the rig root checkout is off limits.

Survey the repository as it is at `HEAD` of the worktree and write
`survey_doc` (`{{output_dir}}/README.md`) — the whole document, replaced
each run. It is the repository's current-state record: what the code does
today, for a reader who has never opened it. Start the file with a top-level
heading; leave frontmatter out — it is stamped in step 3.

Contents, in this order:

- **Overview** — what the system does and for whom. One paragraph.
- **Architecture** — the major components and how they connect; the main
  data and control flows; where the boundaries are. A text diagram is
  welcome when it clarifies.
- **Components** — one section each: responsibility, entry points
  (binaries, handlers, exported packages, CLI verbs), key types and
  modules, data it owns versus reads, dependencies in and out.
- **Cross-cutting** — configuration (env, flags, files and their
  precedence), persistence, auth, observability, build/test/release,
  operational commands.
- **State of the code** — test coverage and what it actually exercises,
  TODOs and dead code, known gaps, anything surprising. Where existing
  docs or comments disagree with the code, say so here.

Cite `path:line` wherever a claim would otherwise be hard to check. Prefer
what the code does over what comments claim. Depth follows the reader's
need: enough to orient and find the right file, not a line-by-line tour.

Then, still in the worktree:

1. Commit the document so `git status` is clean:

       git add "{{output_dir}}/README.md"
       git commit -m "docs: current-state survey of $GC_RIG ($(date -u +%Y-%m-%d))"

2. Stamp the frontmatter and validate it against the pack's schema
   (never type the frontmatter yourself):

       gc hindsight survey stamp <root>

   It prints the stamped block. Commit again:

       git commit -am "docs: stamp current-state frontmatter"

3. Verify `git status` is clean and the file begins with `---`.

Close with `gc.outcome=pass`. If the schema refuses the stamp, or you could
not complete the survey, close with `gc.outcome=fail` and the reason in
the notes.
