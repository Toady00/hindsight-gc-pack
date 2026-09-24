# lint

Opt-in semantic document linting with TypeSafe Jev. This is an experimental
command for explicit local files, including uncommitted drafts. It uses Python's
standard library and the pack's existing Mike Farah `yq` dependency. It does not
install a TypeSafe SDK or require Hindsight credentials, Beads, or an archivist.

From a city importing this pack, or a rig with that city context:

```bash
# Preview the exact state and questions without a key or network access.
gc hindsight lint --dry-run /absolute/path/to/docs/example.md

# With TYPESAFE_API_KEY set, call TypeSafe and print per-rule results.
gc hindsight lint /absolute/path/to/docs/example.md

# Save inputs, model identity, raw probabilities, usage, and latency for review.
gc hindsight lint --json /absolute/path/to/docs/example.md > lint-result.json
```

Pack commands are usable from rigs. When the rig is beneath its city directory,
gc can discover the city by walking up from the working directory. For an
external or sibling checkout, supply the importing city with `GC_CITY`:

```bash
# From the hindsight checkout, whose sibling city is las-vegas:
GC_CITY="$(pwd)/../las-vegas" gc hindsight lint --dry-run docs/adr.memory.ship-trigger.0001.md
```

`GC_CITY` can also be exported in the shell for repeated commands. Existing
city-level pack imports suffice for this command; a rig import is not needed.
The installed gc preserves the caller's working directory, so `docs/example.md`
refers to the rig's document. Without city context, gc may report an unknown flag
such as `--dry-run` rather than explain that the pack was not discovered. Prefer
`GC_CITY` over a pre-binding `--city`, which installed versions can forward into
pack arguments.

You can also run `commands/lint/run.sh` directly from this checkout, with no city
context. This command reads local bytes, unlike
shipping, which reads published Git snapshots. Each explicit invocation without
`--dry-run` sends applicable documents' bodies and selected frontmatter fields to
`https://api.typesafe.ai/v1/systemone`. It does not contact the memory bank.

## Checks

The existing `schemas/docs/validate.py` validates frontmatter first. This PoC
supports the `docs` dialect only. Semantic checks apply according to declarations:

| Rule | Applies when | Finding |
|---|---|---|
| `accepted-draft` | `status: accepted` | Body identifies this document as currently draft or awaiting approval |
| `repo-platform-mandate` | `scope: repo` | Body asserts its own binding platform-wide authority |
| `survey-future-plan` | `type: current-state` | Main account is a future plan rather than observed behavior |
| `gotcha-transient` | `type: gotcha` | Body records only temporary state, without a reusable trap or lesson |

Historical draft notes, drafts of other documents, code examples, quotations,
references to existing platform rules, and survey recommendations are explicitly
distinguished from violations. These distinctions are prompts to evaluate, not
proven model performance. The model supplies a probability per condition; report
messages are fixed rule descriptions, not generated explanations or exact spans.

One document forms one request with its applicable questions evaluated together.
The entire body is preserved. There is no local document-byte or request-byte
cutoff; the original 16,000/28,000-byte PoC caps rejected ordinary project docs
and were removed. The API enforces the selected model's token limits. A preview
reports document and request byte counts for inspection, but does not establish
that the request fits the model's context. No summarization, automatic chunking,
keyword prefilter, or silent truncation hides qualifications. Context rejection
is an error, not a finding; there is no automatic retry with reduced content.
Focused section-based linting remains a separate experiment because some rules
judge the document as a whole. A document with no applicable rules reports
`not-applicable` and consumes no call.

## Results and reproducibility

The default output uses readable check names and explains each result:

- `PASS`: the checked problem was not detected.
- `FINDING`: a likely problem was detected, followed by its description.
- `REVIEW`: the result is inconclusive, followed by what to check manually.
- `SKIP`: the rule does not apply to this document's frontmatter, with the reason.
- `ERROR` or `NOT RUN`: evaluation failed or did not run.

A per-document summary counts passed checks, findings, inconclusive results, and
skipped checks. Model details, thresholds, and probabilities are available through
`--json` rather than printed in the normal report.

Default model: `jev-1.13.0`, pinned for comparable experiments. Use `--model` to
compare another version. Rules have their own `ruleset_version` in JSON reports.
Each report contains the source-byte SHA-256, body start line, exact request,
returned model, probabilities, token usage when supplied, and request latency.
JSON reports include document content; save them where you intend to keep it.

Default `--threshold 0.8` is provisional, not calibrated on this corpus:

- Probability at least 0.8: `finding`.
- Probability at most 0.2: `clear` for this check only.
- Between those bounds: `uncertain`, never silently counted as clear.

Changing `--threshold T` sets the clear boundary to `1 - T`. A Noul has no
separate confidence field. Scores are probabilities of the condition, not degrees
of document quality. A clear check does not verify the document's factual truth.

Exit codes:

- `0`: selected checks clear, preview produced, or no checks apply. Inspect the
  per-document status to distinguish these cases.
- `1`: at least one finding or uncertain check.
- `2`: invalid input, missing credentials, service failure, or malformed response.
  No successful audit is implied. On service failure, later files are `not-run`.

`--timeout` defaults to 30 seconds per request. There are no automatic retries;
rate-limit/overload errors ask you to retry later. HTTP redirects are refused.
Inputs are prepared and validated for the whole invocation before the first call.

## Proving out the experiment

Start with the labeled cases in `test/fixtures/semantic-lint-cases.json`. The
evaluation script materializes those cases in temporary files, uses this command's
implementation, and grades the results. From this checkout:

```bash
python3 test/evaluate_semantic_lint.py --dry-run > lint-preview.json
# With TYPESAFE_API_KEY set, makes ten document requests:
python3 test/evaluate_semantic_lint.py > lint-evaluation.json
```

The evaluator exits 0 when every expected condition is classified correctly,
1 for incorrect or uncertain judgments, and 2 for incomplete/error runs. Dry-run
returns 0 for a valid preview but reports every condition as unscored. Include
real documents you label yourself before looking at the model result.

Compare expected conditions with each check's probability and status. Count
correct detections, false positives, missed violations, and uncertain cases
separately; uncertainty is not a correct classification. Preserve JSON reports to
compare question/model versions, cost, and latency. Rephrase historical notes and
quoted examples to test sensitivity to wording. Offline tests verify plumbing,
not whether Jev understands these cases.

Nothing invokes this command from shipping, maintenance, surveys, hooks, or
orders. It never edits documents, writes bank state, or creates work. Future
integration is a separate decision after evaluation. See the
[proposal](../../docs/discussion.memory.semantic-checks.0001.md) for deferred ideas.
