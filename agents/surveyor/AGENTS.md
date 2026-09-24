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
- Live run `ing-hah` on 2026-09-06 linked graph-v2 members to the root with
  `tracks`, not parent-child edges. `gc bd --rig ingestion list --parent
  ing-hah --status all --limit 0 --json` returned `[]` despite a closed passing
  scope body. Cleanup consequently preserved the worktree for a missing scope,
  not for `publish=none`. Discover members by `gc.root_bead_id` in the rig
  store; the mocked parent query in the offline suite hid this mismatch.
  The regression fixture now distinguishes `tracks` from parent-child links
  and exercises cleanup with metadata from a real compiled formula.
- Surveyor process startup does not prove work was claimed. On 2026-09-07,
  Terraform org and platform starts reached `gc hook --claim --json` but
  drained with `stale_session` for closed session beads `lv-hd9` and `lv-wfn`.
  The platform workflow remained open and unclaimed. Check hook responses and
  step claims separately from dispatcher sessions or generated session titles.
