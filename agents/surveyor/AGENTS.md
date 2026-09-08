# Survey compatibility pitfalls

- Older `gc` builds accepted `order run --var` but materialized formula
  defaults instead of the supplied values. On 2026-09-07 the installed build
  preserved `publish=none` through both order dispatch and formula cook.
  A successful command exit alone does not prove `publish=none`; inspect the
  persisted step descriptions. `test/test_survey_integration.py` tests both
  paths in disposable fixtures, plus the default PR publication mode.
- Cleanup and workflow finalization can both become ready after the worktree
  body closes. Gate cleanup on that body's closed status and `gc.outcome=pass`,
  not on the root outcome. Resolve the body by exact root, `gc.kind=scope`,
  `gc.scope_role=body`, and `gc.step_ref=current-state-survey.worktree`.
- `gc rig list --json` returns an object with `city_path` and `rigs`, including
  an HQ entry marked `hq=true`. Exclude HQ before path matching. Both `GC_RIG`
  and `GC_RIG_ROOT` are real environment names; keep them when entering the
  worktree. These rig orders have `gc.root_store_ref=rig:<rig>`; forcing
  `gc bd --city` selects the wrong store.
- A city-only import does not create each rig's order. Configure
  `[rigs.imports.hindsight]`. The binding-qualified `hindsight.surveyor` target
  resolves in that rig's context to `<rig>/hindsight.surveyor`.
- A file-provider fixture without a rig-local `.gc/beads.json` silently uses
  the legacy shared city store. Seed the rig store to test routing. Never use
  `gc init` for these fixtures: it registers and starts a city.
