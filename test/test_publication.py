"""Publication lifecycle: identity, per-document gates, report sets, and recovery."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

try:
    from .pack_fixture import PACK, SCRIPTS
except ImportError:
    from pack_fixture import PACK, SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import ingestion
import publication
import ship_report
import ship_docs
from ingestion import Error

DERIVE = PACK / "schemas/docs/derive"
REPO = "https://fixture.invalid/rig"
FP = {name: name * 64 for name in "abcdef"}


def candidate(document_id, fingerprint, status="draft", kind="spec", relpath=None, assesses=None, source=None):
    result = dict(namespace="alpha", repository=REPO, relpath=relpath or f"docs/{document_id}.md", commit="c" * 40,
                  ref="refs/heads/main", type=kind, status=status, fingerprint=fingerprint,
                  source_hash=source or fingerprint[:32] + status.ljust(32, "-"))
    if kind == publication.REPORT:
        result.update(assesses=[dict(id=i, fingerprint=f) for i, f in (assesses or {}).items()],
                      outcome="partial", code=[dict(repo="rig", commit="d" * 40)])
    return result


def record(cand, **changes):
    result = publication.publication_record(cand, None, None, "then")
    result.update(changes)
    return result


def actions(decisions):
    return {i: d.action for i, d in decisions.items()}


class PolicyTest(unittest.TestCase):
    """The gates from the per-document lifecycle, without any I/O."""

    def plan(self, candidates, records=(), accepted=()):
        records = {r_id: r for r_id, r in dict(records).items()}
        bank = {i: r["source_hash"] for i, r in records.items() if r.get("visible", True)}
        return publication.plan({c_id: c for c_id, c in candidates.items()}, records, bank, set(accepted))

    def test_drafts_publish_freely_until_first_acceptance(self):
        spec = "alpha.spec.a.0001"
        decisions, sets = self.plan({spec: candidate(spec, FP["a"])})
        self.assertEqual(actions(decisions), {spec: "publish"})
        edited = self.plan({spec: candidate(spec, FP["b"])}, {spec: record(candidate(spec, FP["a"]))})[0]
        self.assertEqual(edited[spec].action, "publish")
        self.assertEqual(edited[spec].kind, "content")
        accepted = self.plan({spec: candidate(spec, FP["b"], "accepted")}, {spec: record(candidate(spec, FP["b"]))})[0]
        self.assertEqual((accepted[spec].action, accepted[spec].kind), ("publish", "status"))
        self.assertEqual(sets, [])

    def test_discussion_migrates_legacy_status_without_acceptance_or_report_gate(self):
        document_id = "alpha.discussion.a.0001"
        current = candidate(document_id, FP["b"], kind="discussion")
        current.pop("status")
        report_id = "alpha.build-report.legacy"
        for status in ["draft", "accepted", "superseded", "deprecated"]:
            with self.subTest(status=status):
                legacy = dict(current, status=status, fingerprint=FP["a"], source_hash=FP["a"])
                # Old publications may have erroneously classified or pinned a discussion.
                published = {document_id: dict(record(current), **legacy, ever_accepted=True),
                             report_id: record(candidate(report_id, FP["e"], kind="build-report",
                                                         assesses={document_id: FP["a"]}))}
                decisions, sets = self.plan({document_id: current}, published, {document_id})
                self.assertEqual(decisions[document_id].action, "publish")
                self.assertEqual(sets, [])
                updated = publication.publication_record(current, published[document_id], None, "now", True)
                self.assertNotIn("status", updated)
                self.assertNotIn("ever_accepted", updated)
        self.assertEqual(self.plan({document_id: current}, accepted={document_id})[0][document_id].action, "publish")

    def test_reports_cannot_pin_discussions(self):
        document_id, report_id = "alpha.discussion.a.0001", "alpha.build-report.one"
        discussion = candidate(document_id, FP["a"], kind="discussion")
        discussion.pop("status")
        report = candidate(report_id, FP["e"], kind="build-report", assesses={document_id: FP["a"]})
        decisions, _ = self.plan({document_id: discussion, report_id: report})
        self.assertEqual(decisions[report_id].action, "refuse")
        self.assertEqual(decisions[document_id].action, "publish")

    def test_legacy_report_in_git_must_drop_discussion_pins_before_publishing(self):
        discussion_id, spec_id, report_id = "alpha.discussion.a.0001", "alpha.spec.a.0001", "alpha.build-report.one"
        discussion = candidate(discussion_id, FP["a"], kind="discussion")
        discussion.pop("status")
        spec = candidate(spec_id, FP["b"], "accepted")
        report = candidate(report_id, FP["e"], kind="build-report",
                           assesses={discussion_id: FP["a"], spec_id: FP["b"]})
        published = {i: record(c) for i, c in [(discussion_id, discussion), (spec_id, spec), (report_id, report)]}
        candidates = {discussion_id: discussion, spec_id: spec, report_id: report}
        decision = self.plan(candidates, published)[0][report_id]
        self.assertEqual(decision.action, "refuse")
        self.assertIn(discussion_id, decision.reason)
        self.assertIn("remove those assesses entries", decision.reason)
        candidates[report_id] = candidate(report_id, FP["f"], kind="build-report", assesses={spec_id: FP["b"]})
        self.assertEqual(self.plan(candidates, published)[0][report_id].action, "publish")

    def test_drafts_after_acceptance_keep_the_last_eligible_revision(self):
        spec = "alpha.spec.a.0001"
        published = {spec: record(candidate(spec, FP["a"], "accepted"))}
        for name, cand in (("revert to draft", candidate(spec, FP["a"])),
                           ("draft edit", candidate(spec, FP["b"]))):
            with self.subTest(name):
                decision = self.plan({spec: cand}, published)[0][spec]
                self.assertEqual(decision.action, "hold")
                self.assertIn("accepted", decision.reason)
        # Acceptance seen only in Git history, between scans, still counts.
        draft_record = {spec: record(candidate(spec, FP["a"]))}
        self.assertEqual(self.plan({spec: candidate(spec, FP["b"])}, draft_record, {spec})[0][spec].action, "hold")
        self.assertEqual(self.plan({spec: candidate(spec, FP["b"])}, {}, {spec})[0][spec].action, "hold")
        # Reacceptance of new content publishes when no report governs it.
        self.assertEqual(self.plan({spec: candidate(spec, FP["b"], "accepted")}, published)[0][spec].action, "publish")

    def test_new_accepted_intent_and_historical_reports_publish_independently(self):
        spec, prd, report = "alpha.spec.a.0001", "alpha.prd.a.0001", "alpha.build-report.omg-1"
        old_spec, old_prd = candidate(spec, FP["a"], "accepted"), candidate(prd, FP["c"], "accepted")
        old_report = candidate(report, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"], prd: FP["c"]})
        published = {spec: record(old_spec), prd: record(old_prd), report: record(old_report)}
        new_spec = candidate(spec, FP["b"], "accepted")
        # An old partial assessment stays valid when accepted scope changes.
        decisions, sets = self.plan({spec: new_spec, prd: old_prd, report: old_report}, published)
        self.assertEqual(decisions[spec].action, "publish")
        self.assertEqual(decisions[report].action, "unchanged")
        self.assertEqual(sets, [])
        new_report = candidate(report, FP["f"], kind=publication.REPORT, assesses={spec: FP["b"], prd: FP["c"]})
        decisions, sets = self.plan({spec: new_spec, prd: old_prd, report: new_report}, published)
        self.assertEqual(actions(decisions), {spec: "publish", prd: "unchanged", report: "publish"})
        self.assertEqual(sets, [])
        # A verified Git revision need not be the bank's current revision.
        draft_edit = candidate(spec, FP["d"])
        wrong = candidate(report, FP["f"], kind=publication.REPORT, assesses={spec: FP["d"], prd: FP["c"]})
        decisions, _ = self.plan({spec: draft_edit, prd: old_prd, report: wrong}, published)
        self.assertEqual((decisions[spec].action, decisions[report].action), ("hold", "publish"))

    def test_report_correction_and_new_proposals_publish_without_a_build(self):
        spec, report, proposal = "alpha.spec.a.0001", "alpha.build-report.omg-1", "alpha.spec.next.0001"
        old_spec = candidate(spec, FP["a"], "accepted")
        old_report = candidate(report, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"]})
        published = {spec: record(old_spec), report: record(old_report)}
        corrected = candidate(report, FP["f"], kind=publication.REPORT, assesses={spec: FP["a"]})
        decisions, sets = self.plan({spec: old_spec, report: corrected, proposal: candidate(proposal, FP["c"])}, published)
        self.assertEqual(actions(decisions), {spec: "unchanged", report: "publish", proposal: "publish"})
        self.assertEqual(sets, [])
        # Retiring or restoring a pinned document keeps its fingerprint.
        retired = self.plan({spec: candidate(spec, FP["a"], "deprecated"), report: old_report}, published)[0]
        self.assertEqual((retired[spec].action, retired[spec].kind), ("publish", "status"))

    def test_old_and_retired_reports_do_not_gate_intent(self):
        spec, first, second = "alpha.spec.a.0001", "alpha.build-report.one", "alpha.build-report.two"
        old_spec = candidate(spec, FP["a"], "accepted")
        reports = {r: candidate(r, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"]}, source=r.ljust(64, "x"))
                   for r in (first, second)}
        published = {spec: record(old_spec), **{r: record(c) for r, c in reports.items()}}
        new_spec = candidate(spec, FP["b"], "accepted")
        updated = candidate(first, FP["f"], kind=publication.REPORT, assesses={spec: FP["b"]})
        decisions, _ = self.plan({spec: new_spec, first: updated, second: reports[second]}, published)
        self.assertEqual((decisions[spec].action, decisions[first].action), ("publish", "publish"))
        # Retirement changes the report's standing, not intent eligibility.
        published[second]["status"] = "deprecated"
        decisions, sets = self.plan({spec: new_spec, first: updated, second: dict(reports[second], status="deprecated")},
                                    published)
        self.assertEqual(decisions[spec].action, "publish")
        second_updated = candidate(second, FP["f"], kind=publication.REPORT, assesses={spec: FP["b"]},
                                   source="z" * 64)
        published[second]["status"] = "draft"
        decisions, sets = self.plan({spec: new_spec, first: updated, second: second_updated}, published)
        self.assertEqual(actions(decisions), {spec: "publish", first: "publish", second: "publish"})
        self.assertEqual(sets, [])

    def test_a_report_correction_does_not_govern_former_pins(self):
        spec, prd, report = "alpha.spec.a.0001", "alpha.prd.a.0001", "alpha.build-report.one"
        published = {spec: record(candidate(spec, FP["a"], "accepted")), prd: record(candidate(prd, FP["c"], "accepted")),
                     report: record(candidate(report, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"], prd: FP["c"]}))}
        dropped = candidate(report, FP["f"], kind=publication.REPORT, assesses={prd: FP["c"]})
        decisions, sets = self.plan({spec: candidate(spec, FP["b"], "accepted"), prd: candidate(prd, FP["c"], "accepted"),
                                     report: dropped}, published)
        self.assertEqual(decisions[spec].action, "publish")
        self.assertEqual(decisions[report].action, "publish")  # a correction of what it still pins
        self.assertEqual(sets, [])

    def test_legacy_withdrawn_report_does_not_gate_intent(self):
        spec, report = "alpha.spec.a.0001", "alpha.build-report.one"
        report_record = record(candidate(report, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"]}), visible=False)
        published = {spec: record(candidate(spec, FP["b"], "accepted")), report: report_record}
        self.assertEqual(self.plan({spec: candidate(spec, FP["c"], "accepted")}, published)[0][spec].action, "publish")
        resumed = candidate(report, FP["f"], kind=publication.REPORT, assesses={spec: FP["b"]})
        decisions, sets = self.plan({spec: candidate(spec, FP["b"], "accepted"), report: resumed}, published)
        self.assertEqual(actions(decisions), {spec: "unchanged", report: "publish"})
        self.assertEqual(sets, [])

    def test_post_acceptance_drafts_hold_even_when_only_updated_at_changed(self):
        spec = "alpha.spec.a.0001"
        published = {spec: record(candidate(spec, FP["a"]))}
        retouched = candidate(spec, FP["a"], source="1" * 64)
        self.assertEqual(self.plan({spec: retouched}, published, {spec})[0][spec].action, "hold")
        self.assertEqual(self.plan({spec: candidate(spec, FP["a"])}, published, {spec})[0][spec].action, "unchanged")

    def test_a_failed_first_publication_by_this_pipeline_is_not_an_unknown_baseline(self):
        spec = "alpha.spec.a.0001"
        cand = candidate(spec, FP["a"])
        self.assertEqual(publication.plan({spec: cand}, {}, {spec: "partial"}, set())[0][spec].action, "refuse")
        self.assertEqual(publication.plan({spec: cand}, {}, {spec: "partial"}, set(), {spec})[0][spec].action, "publish")
        frozen = candidate(spec, FP["a"], "deprecated")
        self.assertEqual(publication.plan({spec: frozen}, {}, {spec: "partial"}, set(), {spec})[0][spec].action, "refuse")

    def test_frozen_documents_accept_only_status_changes_to_published_content(self):
        spec = "alpha.spec.a.0001"
        retired = {spec: record(candidate(spec, FP["a"], "deprecated"), ever_accepted=True)}
        decision = self.plan({spec: candidate(spec, FP["b"], "deprecated")}, retired)[0][spec]
        self.assertEqual(decision.action, "refuse")
        self.assertIn("Revert the file to the published content", decision.reason)
        restored = self.plan({spec: candidate(spec, FP["a"], "accepted")}, retired)[0][spec]
        self.assertEqual((restored.action, restored.kind), ("publish", "status"))
        self.assertEqual(self.plan({spec: candidate(spec, FP["a"], "superseded")}, retired)[0][spec].action, "publish")
        self.assertEqual(self.plan({spec: candidate(spec, FP["a"])}, retired)[0][spec].action, "hold")
        # Retiring must apply to the last published content, not new bytes.
        accepted = {spec: record(candidate(spec, FP["a"], "accepted"))}
        self.assertEqual(self.plan({spec: candidate(spec, FP["b"], "superseded")}, accepted)[0][spec].action, "refuse")

    def test_missing_baselines_never_permit_guessed_retirement_or_identity_changes(self):
        spec = "alpha.spec.a.0001"
        orphan = publication.plan({spec: candidate(spec, FP["a"], "accepted")}, {}, {spec: "hash"}, set())[0]
        self.assertEqual(orphan[spec].action, "refuse")
        self.assertIn("no publication record", orphan[spec].reason)
        self.assertEqual(self.plan({spec: candidate(spec, FP["a"], "deprecated")})[0][spec].action, "refuse")
        published = {spec: record(candidate(spec, FP["a"]))}
        for change in (dict(type="prd"), dict(namespace="beta")):
            with self.subTest(change=change):
                self.assertEqual(self.plan({spec: dict(candidate(spec, FP["a"]), **change)}, published)[0][spec].action,
                                 "refuse")
        # The namespace claim, not each record, binds the repository: an origin
        # rebound on the claim keeps its documents publishable.
        moved_origin = dict(candidate(spec, FP["a"]), repository="https://moved.invalid/rig")
        self.assertEqual(self.plan({spec: moved_origin}, published)[0][spec].action, "unchanged")
        moved = candidate(spec, FP["a"], relpath="docs/new/home.md")
        decision = self.plan({spec: moved}, published)[0][spec]
        self.assertEqual((decision.action, decision.kind), ("publish", "status"))
        self.assertEqual(publication.publication_record(moved, published[spec], None, "now")["moved_from"],
                         published[spec]["relpath"])

    def test_reports_pin_only_their_own_namespace(self):
        report = "alpha.build-report.one"
        cand = candidate(report, FP["e"], kind=publication.REPORT, assesses={"other.spec.a.0001": FP["a"]})
        decision = self.plan({report: cand})[0][report]
        self.assertEqual(decision.action, "refuse")
        self.assertIn("own initiative", decision.reason)

    def test_first_publication_needs_no_coupled_set(self):
        spec, report = "alpha.spec.a.0001", "alpha.build-report.one"
        decisions, sets = self.plan({
            spec: candidate(spec, FP["a"], "accepted"),
            report: candidate(report, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"]})})
        self.assertEqual(actions(decisions), {spec: "publish", report: "publish"})
        self.assertEqual(sets, [])


class SchemaLifecycleTest(unittest.TestCase):
    BASE = ("---\nschema_version: 2\nid: alpha.spec.a.0001\ntype: spec\ntitle: A\nstatus: draft  # note\n"
            "source: agent\nscope: platform\nupdated_at: 2026-08-20T00:00:00Z\n---\nBody\nstatus: accepted\n")

    def derive(self, text):
        result = subprocess.run([str(DERIVE)], input=text, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def fingerprint(self, text):
        verdict = self.derive(text)
        self.assertEqual(verdict["verdict"], "ship", verdict)
        return verdict["lifecycle"]["fingerprint"]

    def test_status_free_discussion_context_tags_and_fingerprint(self):
        text = self.BASE.replace("type: spec", "type: discussion").replace("status: draft  # note\n", "")
        verdict = self.derive(text)
        self.assertEqual(verdict["verdict"], "ship", verdict)
        self.assertNotIn("status", verdict["lifecycle"])
        self.assertFalse(any(t.startswith("status:") for t in verdict["tags"]))
        self.assertIn("conversation history", verdict["context"])
        self.assertIn("not approved direction", verdict["context"])
        self.assertEqual(self.fingerprint(text), self.fingerprint(text.replace("08-20T00", "09-01T07")))
        self.assertNotEqual(self.fingerprint(text), self.fingerprint(text + "Later: defer R2.\n"))
        for status in ["draft", "accepted", "superseded", "deprecated", "null", "recorded"]:
            with self.subTest(status=status):
                bad = text.replace("type: discussion\n", f"type: discussion\nstatus: {status}\n")
                self.assertEqual(self.derive(bad)["verdict"], "refuse")

    def test_only_status_and_updated_at_values_are_masked(self):
        base = self.fingerprint(self.BASE)
        same = self.BASE.replace("status: draft", "status: 'deprecated'").replace("08-20T00", "09-01T07")
        self.assertEqual(self.fingerprint(same), base)
        for name, text in (("source", self.BASE.replace("source: agent", "source: human")),
                           ("status comment", self.BASE.replace("# note", "# other")),
                           ("body status line", self.BASE.replace("Body\nstatus: accepted", "Body\nstatus: draft")),
                           ("id", self.BASE.replace("alpha.spec.a.0001", "alpha.spec.b.0001"))):
            with self.subTest(name):
                self.assertNotEqual(self.fingerprint(text), base)

    def test_ambiguous_frontmatter_is_refused_not_partly_exempted(self):
        for name, text in (
                ("duplicate status", self.BASE.replace("source: agent", "source: agent\nstatus: accepted")),
                ("duplicate other field", self.BASE.replace("source: agent", "source: agent\nsource: human")),
                ("quoted key", self.BASE.replace("status: draft  # note", '"status": draft')),
                ("block scalar", self.BASE.replace("status: draft  # note", "status: >-\n  draft")),
                ("flow mapping", self.BASE.replace("status: draft  # note\n", "").replace(
                    "scope: platform", "scope: platform\nextra: {status: draft}"))):
            with self.subTest(name):
                verdict = self.derive(text)
                if name == "flow mapping":
                    self.assertEqual(verdict["verdict"], "refuse")  # status is then missing entirely
                else:
                    self.assertEqual(verdict["verdict"], "refuse", verdict)

    def test_build_reports_carry_evidence_fields(self):
        report = self.BASE.replace("type: spec", "type: build-report").replace(
            "updated_at: 2026-08-20T00:00:00Z", "updated_at: 2026-08-20T00:00:00Z\noutcome: failed\n"
            f"assesses:\n  - id: alpha.spec.b.0001\n    fingerprint: '{FP['a']}'\ncode:\n  - repo: rig\n    commit: '{'1' * 40}'")
        verdict = self.derive(report)
        self.assertEqual(verdict["lifecycle"]["outcome"], "failed")
        self.assertEqual(verdict["lifecycle"]["assesses"], [dict(id="alpha.spec.b.0001", fingerprint=FP["a"])])
        self.assertIn("IMPLEMENTATION EVIDENCE", verdict["context"])
        self.assertIn("not approval, deployment", verdict["context"])
        for broken in (report.replace("outcome: failed", "outcome: shipped"),
                       report.replace(f"fingerprint: '{FP['a']}'", "fingerprint: short"),
                       report.replace(f"commit: '{'1' * 40}'", f"commit: {'1' * 40}"),
                       self.BASE.replace("updated_at: 2026-08-20T00:00:00Z", "updated_at: 2026-08-20T00:00:00Z\noutcome: passed")):
            with self.subTest(broken=broken[-80:]):
                self.assertEqual(self.derive(broken)["verdict"], "refuse")


class NamespaceTest(unittest.TestCase):
    def test_canonical_keys_collisions_and_id_shape(self):
        self.assertEqual(publication.canonical_namespace("Ingestion"), "ingestion")
        for bad in ("rig", "City", "has.dot", "", "-lead", None):
            self.assertIsNone(publication.canonical_namespace(bad))
        self.assertEqual(publication.namespace_owners("las-vegas", ["ingestion", "terraform_org"]),
                         {"las-vegas": "las-vegas", "ingestion": "ingestion", "terraform_org": "terraform_org"})
        for city, rigs in (("Ingestion", ["ingestion"]), ("alpha", ["alpha"]), ("city-x", ["beta", "Beta"])):
            with self.subTest(city=city, rigs=rigs), self.assertRaisesRegex(ValueError, "both normalize"):
                publication.namespace_owners(city, rigs)
        self.assertEqual(publication.read_namespace("ingestion\n"), "ingestion")
        for bad in ("Ingestion\n", "a\nb\n", "rig\n", "x y\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                publication.read_namespace(bad)
        self.assertIsNone(publication.id_error("ingestion.spec.x.0001", "ingestion"))
        for bad in ("spec.x.0001", "ingestion", "ingestion.rig.spec.x", "ingestion..x"):
            with self.subTest(bad=bad):
                self.assertTrue(publication.id_error(bad, "ingestion"))

    def test_claims_bind_keys_to_repositories_and_survive_renames(self):
        root = dict(status="ok", repository=REPO, toplevel="/rig", namespace="ingestion\n")
        owners = {"/rig": "ingestion"}
        first = ship_docs.resolve_namespaces([root], owners, {})[REPO]
        self.assertEqual((first["key"], first["error"]), ("ingestion", None))
        self.assertEqual(first["claim"]["owner"], "ingestion")
        claims = {"ingestion": dict(repository=REPO)}
        renamed = ship_docs.resolve_namespaces([root], {"/rig": "ingest-v2"}, claims)[REPO]
        self.assertEqual((renamed["key"], renamed["error"], renamed["claim"]), ("ingestion", None, None))
        for name, roots, owner_map, claim_map, expected in (
                ("wrong first key", [dict(root, namespace="other\n")], owners, {}, "canonical name 'ingestion'"),
                ("claimed elsewhere", [root], owners, {"ingestion": dict(repository="https://x.invalid/y")}, "first published by"),
                ("key changed", [dict(root, namespace="ingest\n")], {"/rig": "ingest"}, claims, "never change"),
                ("unregistered", [root], {}, {}, "neither the city nor a registered rig"),
                ("missing file", [dict(root, namespace=None)], owners, {}, ".hindsight-namespace")):
            with self.subTest(name):
                result = ship_docs.resolve_namespaces(roots, owner_map, claim_map)[REPO]
                self.assertIsNone(result["key"])
                self.assertIn(expected, result["error"])


class Store:
    """Value-copy persistence with the BeadsStore interface used by shipping."""
    city, api, bank = "/city", "https://fixture.invalid", "fixture"

    def __init__(self):
        self.rows, self.puts = {}, []

    def get(self, kind, document_id=""):
        return deepcopy(self.rows.get((kind, document_id)))

    def put(self, kind, document_id, data):
        self.puts.append((kind, document_id))
        self.rows[kind, document_id] = deepcopy(dict(data, document_id=document_id) if document_id else data)
        return "record"

    def list_records(self, kind):
        return [deepcopy(v) for (k, _), v in sorted(self.rows.items()) if k == kind]

    def list_documents(self):
        return self.list_records("document")

    def current_work(self):
        return ""


class Bank:
    """A fake Hindsight HTTP API for the production Ingestor.

    Retains complete or fail per document; a failing retain stamps the bank's
    content hash first, as a failed streaming retain can. ``events`` records
    the order in which documents become visible or disappear.
    """

    def __init__(self, store):
        self.store, self.docs, self.events, self.fail = store, {}, [], set()
        self.operations, self.deadline, self.stamp_failures = {}, None, True

    def drain(self, timeout):
        pass

    def capabilities(self):
        pass

    def inventory(self):
        return [dict(id=i, document_metadata=dict(content_hash=h)) for i, h in sorted(self.docs.items())]

    def document_ids(self):
        return set(self.docs)

    def _run(self, op_id):
        item = self.operations[op_id]["item"]
        document_id, digest = item["document_id"], item["metadata"]["content_hash"]
        if document_id not in self.fail or self.stamp_failures:
            self.docs[document_id] = digest
        if document_id in self.fail:
            self.operations[op_id]["status"] = "failed"
        else:
            self.operations[op_id]["status"] = "completed"
            self.events.append(("retain", document_id))

    def request(self, method, suffix, payload=None):
        if method == "DELETE":
            document_id = suffix.split("/", 1)[1]
            self.events.append(("withdraw", document_id))
            self.docs.pop(document_id, None)
            return {"success": True}
        if suffix == "memories":
            op_id = payload["operation_id"]
            self.operations[op_id] = dict(item=deepcopy(payload["items"][0]), status="pending")
            self._run(op_id)
            return {"operation_id": op_id}
        if suffix.endswith("/retry"):
            op_id = suffix.split("/")[1]
            self.events.append(("retry", self.operations[op_id]["item"]["document_id"]))
            self._run(op_id)
            return {"success": True, "operation_id": op_id}
        raise AssertionError((method, suffix))

    def operation(self, op_id):
        if op_id not in self.operations:
            return dict(operation_id=op_id, status="not_found")
        return dict(operation_id=op_id, status=self.operations[op_id]["status"])


class ShipFlowTest(unittest.TestCase):
    """Real schema, policy and orchestration over in-memory store and bank."""

    def setUp(self):
        self.store = Store()
        self.bank = Bank(self.store)
        self.files = {}
        self.history = {}
        self.legacies = set()
        self.commit = "c" * 40
        self.accepted = []
        for name, value in (("BeadsStore", Mock(return_value=self.store)),
                            ("API", Mock(return_value=self.bank)),
                            ("require_writer", Mock()), ("ship_lock", Mock(side_effect=lambda s: nullcontext())),
                             ("snapshot", Mock(side_effect=self.snapshot)),
                             ("RevisionHistory", Mock(side_effect=lambda *args: Mock(
                                 lookup=lambda i, f: self.history.get((i, f)),
                                 is_legacy=lambda i, f: (i, f) in self.legacies))),
                            ("load_registry", Mock(return_value=dict(city_name="city", rigs=[dict(name="alpha", path="/rig")]))),
                            ("owners", Mock(return_value={"/rig": "alpha"}))):
            p = patch.object(ship_docs, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"HINDSIGHT_API_URL": "https://fixture.invalid", "HINDSIGHT_BANK": "fixture",
                                      "PATH": os.environ["PATH"], "HINDSIGHT_WRITER": "archivist",
                                      "GC_SESSION_ID": "fixture-session"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def snapshot(self, roots, ref):
        root = dict(root="/rig/docs", status="ok", detail="", repo="rig", repository=REPO, prefix="docs",
                    ref="refs/heads/main", commit=self.commit, toplevel="/rig", namespace="alpha\n",
                    accepted_ids=list(self.accepted))
        documents = [dict(content=text, repo="rig", repository=REPO, relpath=path, ref=root["ref"],
                          commit=root["commit"], root=root["root"]) for path, text in sorted(self.files.items())]
        for document in documents:
            verdict = ship_docs.derive(document, DERIVE)
            if verdict["verdict"] == "ship":
                lifecycle = verdict["lifecycle"]
                self.history[verdict["document_id"], lifecycle["fingerprint"]] = lifecycle["type"]
        return dict(roots=[root], documents=documents, errors=0)

    def doc(self, name, status, body, kind="spec", extra=""):
        self.files[f"docs/{name}.md"] = (
            f"---\nschema_version: 2\nid: alpha.{kind}.{name}.0001\ntype: {kind}\ntitle: {name}\nstatus: {status}\n"
            f"source: agent\nscope: platform\nupdated_at: 2026-08-20T00:00:00Z\n{extra}---\n{body}\n")
        return f"alpha.{kind}.{name}.0001"

    def fingerprint(self, name):
        result = subprocess.run([str(DERIVE)], input=self.files[f"docs/{name}.md"], capture_output=True,
                                text=True, check=True)
        return json.loads(result.stdout)["lifecycle"]["fingerprint"]

    def report(self, body, *pinned):
        pins = "".join(f"  - id: alpha.spec.{name}.0001\n    fingerprint: '{self.fingerprint(name)}'\n" for name in pinned)
        return self.doc("omg", "draft", body, kind="build-report",
                        extra=f"outcome: partial\nassesses:\n{pins}code:\n  - repo: rig\n    commit: '{'1' * 40}'\n")

    def ship(self, *args, code=0):
        with patch.object(sys, "argv", ["ship_docs.py", *args, "/rig/docs"]), redirect_stdout(io.StringIO()) as out, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(ship_docs.main(), code, out.getvalue())
        return out.getvalue()

    def published(self, document_id):
        return self.store.get("document", document_id)["publication"]

    def test_discussion_ships_repeatedly_while_accepted_intent_drafts_are_held(self):
        discussion = self.doc("conversation", "draft", "Discuss R1 and R2", kind="discussion")
        path = "docs/conversation.md"
        self.files[path] = self.files[path].replace("status: draft\n", "")
        spec = self.doc("a", "accepted", "Build only R1")
        self.accepted.append(discussion)  # A legacy accepted revision in published Git.
        self.ship()
        self.assertNotIn("status", self.published(discussion))
        self.bank.events.clear()
        self.files[path] += "Later: R2 is deferred, preserve its earlier rationale.\n"
        self.doc("a", "draft", "Unapproved R1 revision")
        self.assertIn("HELD", self.ship())
        self.assertEqual(self.bank.events, [("retain", discussion)])
        item = next(v["item"] for v in reversed(list(self.bank.operations.values()))
                    if v["item"]["document_id"] == discussion)
        self.assertIn("R2 is deferred", item["content"])
        self.assertFalse(any(t.startswith("status:") for t in item["tags"]))
        self.assertEqual(self.published(spec)["status"], "accepted")

    def test_partial_assessment_remains_valid_as_accepted_scope_changes(self):
        spec = self.doc("a", "accepted", "R1 v1")
        report = self.report("R1 implemented", "a")
        self.ship()
        self.assertEqual(self.bank.events, [("retain", spec), ("retain", report)])
        self.assertEqual(self.store.get("namespace", "alpha")["repository"], REPO)
        self.assertTrue(self.published(spec)["ever_accepted"])
        old_report = deepcopy(self.published(report))
        # Drafts still hold, but acceptance publishes independently of reports.
        self.bank.events.clear()
        self.doc("a", "draft", "R1 v2")
        output = self.ship()
        self.assertIn("HELD", output)
        self.doc("a", "accepted", "R1 v2")
        self.ship()
        self.assertEqual(self.bank.events, [("retain", spec)])
        self.assertEqual(self.published(report), old_report)
        self.assertIn(report, self.bank.docs)
        retained_spec = next(v["item"] for v in reversed(list(self.bank.operations.values()))
                             if v["item"]["document_id"] == spec)
        retained_report = next(v["item"] for v in self.bank.operations.values()
                               if v["item"]["document_id"] == report)
        self.assertEqual(retained_spec["metadata"]["fingerprint"], self.fingerprint("a"))
        self.assertIn(old_report["assesses"][0]["fingerprint"], retained_report["context"])
        self.assertNotEqual(retained_spec["metadata"]["fingerprint"], old_report["assesses"][0]["fingerprint"])
        self.bank.events.clear()
        # A later assessment updates the report without withdrawing anything.
        self.report("R1 partially implemented", "a")
        self.ship()
        self.assertEqual(self.bank.events, [("retain", report)])
        self.assertEqual(self.published(spec)["fingerprint"], self.fingerprint("a"))
        self.assertTrue(self.published(report)["visible"])
        self.assertEqual(self.published(report)["assesses"][0]["fingerprint"], self.fingerprint("a"))

    def test_failed_intent_retain_does_not_hold_a_verified_report(self):
        spec = self.doc("a", "accepted", "R1 v1")
        report = self.report("R1 implemented", "a")
        self.ship()
        self.doc("a", "accepted", "R1 v2")
        self.report("R1 partially implemented", "a")
        self.bank.fail.add(spec)
        self.bank.events.clear()
        output = self.ship(code=1)
        # Git establishes the assessed revision even if its own retain fails.
        self.assertEqual(self.bank.events, [("retain", report)])
        self.assertIn(report, self.bank.docs)
        self.assertTrue(self.published(report)["visible"])
        self.bank.fail.clear()
        self.bank.events.clear()
        self.ship()
        # The same planned revision resumes by retrying its original operation.
        self.assertEqual(self.bank.events, [("retry", spec), ("retain", spec)])
        self.assertTrue(self.published(report)["visible"])
        self.assertEqual(self.store.list_records("set"), [])

    def test_removed_report_does_not_hold_a_later_intent_revision(self):
        self.doc("a", "accepted", "R1 v1")
        report = self.report("R1 implemented", "a")
        self.ship()
        spec = self.doc("a", "accepted", "R1 v2")
        self.report("R1 partial", "a")
        self.bank.fail.add(spec)
        self.ship(code=1)
        del self.files["docs/omg.md"]
        self.bank.fail.clear()
        self.doc("a", "accepted", "R1 v3")
        self.bank.events.clear()
        output = self.ship()
        self.assertEqual(self.bank.events, [("retain", spec)])
        self.assertIn(report, self.bank.docs)

    def test_failed_planned_publication_is_not_replayed_after_the_plan_changes(self):
        spec = self.doc("a", "accepted", "R1 v1")
        report = self.report("R1 implemented", "a")
        self.ship()
        old_report = self.files["docs/omg.md"]
        self.doc("a", "accepted", "R1 v2")
        self.report("R1 partial", "a")
        self.bank.fail.add(spec)
        self.ship(code=1)
        # The author abandons the change; the extraction would now succeed.
        self.bank.fail.clear()
        self.doc("a", "accepted", "R1 v1")
        self.files["docs/omg.md"] = old_report
        self.bank.events.clear()
        output = self.ship()
        self.assertNotIn(("retry", spec), self.bank.events)
        self.assertIn("ABANDONED", output)
        self.assertNotIn("payload", self.store.get("document", spec)["abandoned_attempt"])
        self.assertEqual(self.published(spec)["fingerprint"], self.fingerprint("a"))
        # The bank copy is repaired independently of the historical report.
        self.assertEqual(self.bank.events, [("retain", spec), ("retain", report)])

    def test_failed_first_publication_retries_and_claims_namespace_only_on_success(self):
        spec = self.doc("a", "draft", "R1")
        self.bank.fail.add(spec)
        self.ship(code=1)
        self.assertIsNone(self.store.get("namespace", "alpha"))
        self.assertIn(spec, self.bank.docs)  # partially stamped by the failed retain
        self.bank.fail.clear()
        self.ship()
        self.assertEqual(self.published(spec)["status"], "draft")
        self.assertEqual(self.store.get("namespace", "alpha")["repository"], REPO)

    def test_an_interrupted_pending_attempt_is_settled_not_retried_after_the_plan_changes(self):
        spec = self.doc("a", "accepted", "R1 v1")
        report = self.report("R1 implemented", "a")
        self.ship()
        published_spec, old_report = self.files["docs/a.md"], self.files["docs/omg.md"]
        self.doc("a", "accepted", "R1 v2")
        self.report("R1 partial", "a")
        self.bank.fail.add(spec)
        self.ship(code=1)
        # As if that scan was interrupted before it observed the failure.
        state = self.store.get("document", spec)
        state["attempt"]["state"] = "pending"
        self.store.put("document", spec, state)
        # The author abandons the change; a retry would now succeed.
        self.bank.fail.clear()
        self.doc("a", "draft", "R1 v3")
        self.files["docs/omg.md"] = old_report
        self.bank.events.clear()
        output = self.ship(code=1)
        self.assertEqual(self.bank.events, [("retain", report)])
        self.assertEqual(self.store.get("document", spec)["attempt"]["state"], "failed")
        self.assertIn("does not hold this document's published revision", output)
        # Restoring the published content repairs the bank and returns the report.
        self.files["docs/a.md"] = published_spec
        self.bank.events.clear()
        output = self.ship()
        self.assertIn("ABANDONED", output)
        self.assertEqual(self.bank.events, [("retain", spec)])

    def test_abandoning_a_failure_that_never_reached_the_bank_leaves_health_resolved(self):
        spec = self.doc("a", "accepted", "R1 v1")
        self.report("R1 implemented", "a")
        self.ship()
        published_spec, old_report = self.files["docs/a.md"], self.files["docs/omg.md"]
        self.doc("a", "accepted", "R1 v2")
        self.report("R1 partial", "a")
        self.bank.fail.add(spec)
        self.bank.stamp_failures = False
        self.ship(code=1)
        self.bank.fail.clear()
        self.files["docs/a.md"], self.files["docs/omg.md"] = published_spec, old_report
        self.ship("--full-scan")
        self.assertIn("abandoned_attempt", self.store.get("document", spec))
        self.assertEqual(ship_report.status(self.store)["unresolved_documents"], [])

    def test_a_repeating_failure_does_not_block_a_later_revision(self):
        spec = self.doc("a", "draft", "R1 v1")
        self.bank.fail.add(spec)
        self.ship(code=1)
        self.ship(code=1)  # the same revision retries and fails again
        self.bank.fail.clear()
        self.doc("a", "draft", "R1 v2 fixes the extraction problem")
        self.bank.events.clear()
        self.ship()
        self.assertEqual(self.bank.events, [("retain", spec)])
        self.assertEqual(self.published(spec)["fingerprint"], self.fingerprint("a"))

    def test_a_former_report_pin_never_governs_new_intent(self):
        spec = self.doc("a", "accepted", "R1 v1")
        self.doc("b", "accepted", "R2 v1")
        report = self.report("R1 and R2 implemented", "a", "b")
        self.ship()
        self.report("R2 implemented", "b")  # a correction that drops spec a
        self.ship()
        self.assertNotIn("governs", self.published(report))
        self.doc("a", "accepted", "R1 v2")
        self.bank.events.clear()
        self.ship()
        self.assertEqual(self.bank.events, [("retain", spec)])
        self.bank.events.clear()
        self.report("R1 partial, R2 implemented", "a", "b")
        self.ship()
        self.assertEqual(self.bank.events, [("retain", report)])

    def test_dry_run_previews_without_writes(self):
        self.doc("a", "accepted", "R1")
        self.report("R1 implemented", "a")
        output = self.ship("--dry-run")
        self.assertIn("WOULD SHIP", output)
        self.assertEqual(self.store.puts, [])
        self.assertEqual(self.bank.events, [])

    def test_unknown_report_revision_is_refused_without_blocking_accepted_intent(self):
        spec = self.doc("a", "accepted", "R1 v1")
        report = self.report("Unsupported assessment", "a")
        self.files["docs/omg.md"] = self.files["docs/omg.md"].replace(self.fingerprint("a"), "f" * 64)
        self.assertIn("not a valid current or historical document", self.ship(code=1))
        self.assertEqual(self.bank.events, [("retain", spec)])
        self.assertNotIn(report, self.bank.docs)

    def test_unchanged_documents_do_not_retain_after_unrelated_branch_push(self):
        self.doc("a", "accepted", "R1 v1")
        self.report("R1 partial", "a")
        self.ship()
        self.bank.events.clear()
        self.commit = "d" * 40
        self.ship()
        self.assertEqual(self.bank.events, [])

    def test_namespaced_report_preserves_attested_pre_namespace_assessed_identity(self):
        self.doc("a", "accepted", "New accepted scope")
        report = self.report("Prior partial assessment", "a")
        old_id, old_fp = "spec.ingestion.initial-ingestion-technical.0001", "a" * 64
        self.files["docs/omg.md"] = self.files["docs/omg.md"].replace("alpha.spec.a.0001", old_id)
        self.files["docs/omg.md"] = self.files["docs/omg.md"].replace(self.fingerprint("a"), old_fp)
        self.history[old_id, old_fp] = "spec"
        self.legacies.add((old_id, old_fp))
        self.ship()
        self.assertEqual(self.published(report)["assesses"], [dict(id=old_id, fingerprint=old_fp)])
        self.assertEqual(self.published(report)["legacy_assesses"],
                         [dict(id=old_id, fingerprint=old_fp, repository=REPO)])
        item = next(v["item"] for v in self.bank.operations.values() if v["item"]["document_id"] == report)
        self.assertIn(REPO, item["context"])
        self.assertEqual(json.loads(item["metadata"]["legacy_assesses"]),
                         self.published(report)["legacy_assesses"])
        self.assertNotIn(old_id, self.bank.docs)  # Referencing history does not publish a legacy document.
        self.legacies.clear()
        self.assertIn("lacks exact pre-namespace historical proof", self.ship(code=1))

    def test_schema_cannot_self_authorize_a_foreign_assessed_identity(self):
        self.doc("a", "accepted", "Scope")
        self.report("Unsupported foreign assessment", "a")
        fp = self.fingerprint("a")
        self.files["docs/omg.md"] = self.files["docs/omg.md"].replace("alpha.spec.a.0001", "other.spec.a.0001")
        self.files["docs/omg.md"] = self.files["docs/omg.md"].replace(
            "outcome: partial", "outcome: partial\nlegacy_references: [other.spec.a.0001]")
        self.history["other.spec.a.0001", fp] = "spec"
        self.assertIn("lacks exact pre-namespace historical proof", self.ship(code=1))

    def test_prepared_legacy_withdrawal_is_not_replayed_and_report_is_restored(self):
        self.doc("a", "accepted", "R1 v1")
        report = self.report("R1 partially implemented", "a")
        self.ship()
        state = self.store.get("document", report)
        state["publication"].update(visible=False, governs=["alpha.spec.a.0001"])
        state["withdrawal"] = dict(state="prepared", set="legacy-set")
        self.store.put("document", report, state)
        self.bank.docs.pop(report)
        self.doc("a", "accepted", "R1 v2")
        self.bank.events.clear()
        self.ship()
        self.assertEqual(self.bank.events, [("retain", "alpha.spec.a.0001"), ("retain", report)])
        self.assertTrue(self.published(report)["visible"])
        self.assertNotIn("governs", self.published(report))
        self.assertNotIn("withdrawal", self.store.get("document", report))

    def test_staged_record_advances_only_after_confirmed_retain(self):
        spec = self.doc("a", "draft", "R1")
        self.bank.fail.add(spec)
        self.ship(code=1)
        state = self.store.get("document", spec)
        self.assertNotIn("publication", state)
        self.assertIn("publication_pending", state)
        # A later scan's recovery completes the attempt; the staged record follows.
        state.update(attempt=dict(state="succeeded", source_hash=state["publication_pending"]["source_hash"]),
                     last_success=dict(source_hash=state["publication_pending"]["source_hash"]))
        self.store.put("document", spec, state)
        self.assertTrue(ship_docs.promote(self.store, spec))
        self.assertEqual(self.published(spec)["status"], "draft")

    def test_namespace_and_scan_level_identity_errors(self):
        self.doc("a", "draft", "R1")
        self.files["docs/a.md"] = self.files["docs/a.md"].replace("alpha.spec.a", "spec.a")
        self.assertIn("namespace", self.ship(code=1))
        self.assertEqual(self.bank.events, [])
        ship_docs.load_registry.return_value = dict(city_name="Alpha", rigs=[dict(name="alpha", path="/rig")])
        self.ship(code=2)


class WithdrawalTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"HINDSIGHT_WRITER": "archivist", "GC_SESSION_ID": "s"})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store()
        self.store.put("document", "alpha.build-report.one", dict(publication=dict(visible=True)))
        self.api = Mock(deadline=None)
        self.present = {"alpha.build-report.one"}
        self.api.document_ids.side_effect = lambda: set(self.present)
        self.ingestor = ingestion.Ingestor(self.store, self.api)

    def test_withdrawal_is_confirmed_by_absence_and_resumable(self):
        self.api.request.side_effect = lambda method, suffix, payload=None: self.present.clear() or {}
        self.assertTrue(self.ingestor.withdraw("alpha.build-report.one", "set-1"))
        state = self.store.get("document", "alpha.build-report.one")
        self.assertEqual(state["withdrawal"]["state"], "done")
        self.assertFalse(state["publication"]["visible"])
        self.assertEqual(self.api.request.call_args.args[:2], ("DELETE", "documents/alpha.build-report.one"))
        self.assertFalse(self.ingestor.recover_withdrawal("alpha.build-report.one"))

    def test_lost_acknowledgement_and_unconfirmed_delete(self):
        def lost(*args, **kwargs):
            self.present.clear()
            raise Error("Hindsight HTTP request failed; outcome unknown")
        self.api.request.side_effect = lost
        self.assertTrue(self.ingestor.withdraw("alpha.build-report.one", "set-1"))
        self.present.add("alpha.build-report.one")
        self.store.put("document", "alpha.build-report.one", dict(publication=dict(visible=True)))
        self.api.request.side_effect = Error("Hindsight HTTP request failed; outcome unknown")
        with self.assertRaisesRegex(Error, "unconfirmed"):
            self.ingestor.withdraw("alpha.build-report.one", "set-2")
        self.assertEqual(self.store.get("document", "alpha.build-report.one")["withdrawal"]["state"], "prepared")
        self.api.request.side_effect = lambda *a, **k: self.present.clear() or {}
        self.assertTrue(self.ingestor.recover_withdrawal("alpha.build-report.one"))
        self.assertEqual(self.store.get("document", "alpha.build-report.one")["withdrawal"]["set"], "set-2")


class CheckCommandTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name).resolve()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HINDSIGHT_"))}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, PYTHONDONTWRITEBYTECODE="1")
        self.git("init", "-q")
        (self.repo / ".hindsight-namespace").write_text("alpha\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs/a.md").write_text(SchemaLifecycleTest.BASE)

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                        "-C", str(self.repo), *args], env=self.env, check=True, capture_output=True)

    def check(self, *args, code=0):
        result = subprocess.run([str(PACK / "commands/check/run.sh"), *args], cwd=self.repo, env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_worktree_revision_and_fingerprint_modes(self):
        result = self.check("--fingerprint")
        self.assertRegex(result.stdout, r"^[0-9a-f]{64}  alpha\.spec\.a\.0001  docs/a\.md$")
        self.git("add", "-A")
        self.git("commit", "-qm", "docs")
        (self.repo / "docs/b.md").write_text(SchemaLifecycleTest.BASE)
        result = self.check(code=1)
        self.assertIn("ID is claimed by 2 files", result.stderr)
        self.assertIn("Repair:", result.stderr)
        self.check("--rev", "HEAD")
        self.check("--rev", "HEAD", ".")
        # Selecting one directory still sees duplicates elsewhere in docs/.
        (self.repo / "docs/sub").mkdir()
        (self.repo / "docs/b.md").rename(self.repo / "docs/sub/b.md")
        result = self.check("docs/sub", code=1)
        self.assertIn("docs/sub/b.md", result.stderr)
        self.assertNotIn("ERROR docs/a.md", result.stderr)
        (self.repo / "docs/sub/b.md").unlink()
        (self.repo / ".hindsight-namespace").write_text("other\n")
        self.assertIn("must start with this repository's namespace", self.check(code=1).stderr)

    def write_report(self, document_id, fingerprint):
        report = SchemaLifecycleTest.BASE.replace("id: alpha.spec.a.0001", "id: alpha.build-report.one")
        report = report.replace("type: spec", "type: build-report").replace(
            "scope: platform", f"scope: platform\noutcome: partial\nassesses:\n"
            f"  - id: {document_id}\n    fingerprint: '{fingerprint}'\ncode:\n"
            f"  - repo: rig\n    commit: '{'1' * 40}'")
        (self.repo / "docs/report.md").write_text(report)

    def test_historical_report_survives_updated_moved_and_deleted_spec(self):
        old_fp = self.check("--fingerprint").stdout.split()[0]
        self.git("add", "-A")
        self.git("commit", "-qm", "original spec")
        self.write_report("alpha.spec.a.0001", old_fp)
        path = self.repo / "docs/a.md"
        path.write_text(path.read_text().replace("Body", "Changed requirements"))
        self.git("add", "-A")
        self.git("commit", "-qm", "new scope and historical partial report")
        self.assertNotIn("WARN", self.check().stderr)
        moved = self.repo / "docs/spec moved\nwith newline.MD"
        path.rename(moved)
        self.git("add", "-A")
        self.git("commit", "-qm", "move spec")
        moved.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "remove current copy")
        self.check("--rev", "HEAD")
        self.check("docs/report.md")
        self.write_report("alpha.spec.a.0001", "f" * 64)
        self.assertIn("not a valid current or historical document", self.check(code=1).stderr)

    def test_private_branch_revision_and_historical_discussion_are_not_valid_pins(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "original")
        self.git("branch", "baseline")
        self.git("checkout", "-qb", "private")
        path = self.repo / "docs/a.md"
        path.write_text(path.read_text().replace("Body", "Private requirements"))
        private_fp = self.check("--fingerprint").stdout.split()[0]
        self.git("add", "-A")
        self.git("commit", "-qm", "private spec")
        self.git("checkout", "baseline")
        self.write_report("alpha.spec.a.0001", private_fp)
        self.assertIn("not a valid current or historical document", self.check(code=1).stderr)
        self.git("merge", "--no-ff", "-s", "ours", "-m", "publish ancestry", "private")
        self.check()  # The side-parent revision now belongs to selected ancestry.
        # Discussion history is real, but cannot establish assessed requirements.
        path.write_text(path.read_text().replace("type: spec", "type: discussion")
                        .replace("status: draft  # note\n", ""))
        (self.repo / "docs/report.md").unlink()
        discussion_fp = self.check("--fingerprint").stdout.split()[0]
        self.git("add", "-A")
        self.git("commit", "-qm", "discussion")
        path.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "no current discussion")
        self.write_report("alpha.spec.a.0001", discussion_fp)
        self.assertIn("discussion", self.check(code=1).stderr)

    def test_old_required_fields_and_malformed_out_of_root_history(self):
        notes = self.repo / "notes"
        notes.mkdir()
        legacy = SchemaLifecycleTest.BASE.replace("alpha.spec.a.0001", "alpha.spec.legacy.0001")
        legacy = legacy.replace("title: A\n", "").replace("source: agent\n", "")
        (notes / "legacy.md").write_text(legacy)
        (notes / "broken.md").write_text(SchemaLifecycleTest.BASE.replace("type: spec", "type: build-report")
                                         .replace("scope: platform", "scope: platform\noutcome: [passed]"))
        self.git("add", "-A")
        self.git("commit", "-qm", "old and malformed historical records")
        (notes / "legacy.md").unlink()
        (notes / "broken.md").unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "remove old files")
        masked = legacy.replace("status: draft", "status: __hindsight_lifecycle_mask__")
        masked = masked.replace("updated_at: 2026-08-20T00:00:00Z", "updated_at: __hindsight_lifecycle_mask__")
        self.write_report("alpha.spec.legacy.0001", hashlib.sha256(masked.encode()).hexdigest())
        self.check()
        self.write_report("alpha.spec.legacy.0001", "f" * 64)
        result = self.check(code=1)
        self.assertNotIn("Traceback", result.stderr)

    def test_duplicate_historical_frontmatter_and_treeish_revision_are_refused(self):
        bad = SchemaLifecycleTest.BASE.replace("type: spec", "type: spec\nid: alpha.spec.other.0001")
        path = self.repo / "docs/duplicate.md"
        path.write_text(bad)
        self.git("add", "-A")
        self.git("commit", "-qm", "ambiguous historical identity")
        path.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "remove ambiguous document")
        self.write_report("alpha.spec.a.0001", hashlib.sha256(bad.encode()).hexdigest())
        self.check(code=1)
        result = self.check("--rev", "HEAD^{tree}", code=2)
        self.assertNotIn("Traceback", result.stderr)

    def test_shallow_history_reports_missing_ancestors(self):
        old_fp = self.check("--fingerprint").stdout.split()[0]
        self.git("add", "-A")
        self.git("commit", "-qm", "assessed revision")
        path = self.repo / "docs/a.md"
        path.write_text(path.read_text().replace("Body", "New scope"))
        self.write_report("alpha.spec.a.0001", old_fp)
        self.git("add", "-A")
        self.git("commit", "-qm", "new scope")
        shallow = self.repo / "shallow-clone"
        self.git("clone", "--depth=1", self.repo.as_uri(), str(shallow))
        self.repo = shallow
        self.assertIn("shallow Git history", self.check(code=1).stderr)

    def test_pre_namespace_assessed_id_is_valid_without_renaming_history(self):
        old_id = "spec.ingestion.initial-ingestion-technical.0001"
        path = self.repo / "docs/a.md"
        old = path.read_text().replace("alpha.spec.a.0001", old_id)
        old_fp = ship_docs.derive(dict(content=old), DERIVE)["lifecycle"]["fingerprint"]
        path.write_text(old)
        (self.repo / ".hindsight-namespace").unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "pre-namespace spec")
        path.write_text(SchemaLifecycleTest.BASE.replace("Body", "New scope"))
        (self.repo / ".hindsight-namespace").write_text("alpha\n")
        self.write_report(old_id, old_fp)
        self.git("add", "-A")
        self.git("commit", "-qm", "declare namespace and report historical work")
        before = (self.repo / "docs/report.md").read_bytes()
        self.check()
        self.check("--rev", "HEAD")
        self.assertEqual((self.repo / "docs/report.md").read_bytes(), before)
        self.write_report(old_id, "f" * 64)
        self.assertIn("lacks exact pre-namespace historical proof", self.check(code=1).stderr)
        self.write_report(old_id, old_fp)
        report = self.repo / "docs/report.md"
        report.write_text(report.read_text().replace("id: alpha.build-report.one", "id: build-report.old"))
        self.assertIn("must start with this repository's namespace", self.check(code=1).stderr)

    def test_post_namespace_type_first_identity_is_not_a_legacy_exemption(self):
        old_id = "spec.ingestion.initial-ingestion-technical.0001"
        self.git("add", "-A")
        self.git("commit", "-qm", "namespace established")
        path = self.repo / "docs/a.md"
        text = path.read_text().replace("alpha.spec.a.0001", old_id)
        fp = ship_docs.derive(dict(content=text), DERIVE)["lifecycle"]["fingerprint"]
        path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "late unnamespaced identity")
        path.write_text(SchemaLifecycleTest.BASE)
        self.write_report(old_id, fp)
        self.assertIn("lacks exact pre-namespace historical proof", self.check(code=1).stderr)

    def test_foreign_namespace_in_pre_namespace_history_is_not_a_legacy_id(self):
        foreign = SchemaLifecycleTest.BASE.replace("alpha.spec.a.0001", "other.spec.a.0001")
        fp = ship_docs.derive(dict(content=foreign), DERIVE)["lifecycle"]["fingerprint"]
        (self.repo / "docs/a.md").write_text(foreign)
        (self.repo / ".hindsight-namespace").unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "foreign-namespaced historical document")
        (self.repo / "docs/a.md").write_text(SchemaLifecycleTest.BASE)
        (self.repo / ".hindsight-namespace").write_text("alpha\n")
        self.write_report("other.spec.a.0001", fp)
        self.assertIn("lacks exact pre-namespace historical proof", self.check(code=1).stderr)

    def test_namespace_removal_cannot_reset_a_modern_identity_lineage(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "namespace established")
        old_id = "spec.ingestion.initial-ingestion-technical.0001"
        text = SchemaLifecycleTest.BASE.replace("alpha.spec.a.0001", old_id)
        fp = ship_docs.derive(dict(content=text), DERIVE)["lifecycle"]["fingerprint"]
        (self.repo / ".hindsight-namespace").unlink()
        (self.repo / "docs/a.md").write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "remove namespace and introduce old-style identity")
        (self.repo / ".hindsight-namespace").write_text("alpha\n")
        (self.repo / "docs/a.md").write_text(SchemaLifecycleTest.BASE)
        self.write_report(old_id, fp)
        self.assertIn("lacks exact pre-namespace historical proof", self.check(code=1).stderr)

    def test_late_pre_adoption_branch_and_orphan_merge_are_not_legacy_evidence(self):
        namespace = self.repo / ".hindsight-namespace"
        namespace.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "pre-adoption root")
        self.git("branch", "late-side")
        self.git("checkout", "-qb", "adopted")
        namespace.write_text("alpha\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "namespace adoption")
        self.git("checkout", "late-side")
        old_id = "spec.evil.0001"
        text = SchemaLifecycleTest.BASE.replace("alpha.spec.a.0001", old_id)
        fp = ship_docs.derive(dict(content=text), DERIVE)["lifecycle"]["fingerprint"]
        (self.repo / "docs/late.md").write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "late old-style identity")
        self.git("checkout", "adopted")
        self.git("merge", "--no-ff", "-m", "merge late side", "late-side")
        self.write_report(old_id, fp)
        self.assertIn("lacks exact pre-namespace historical proof", self.check(code=1).stderr)
        (self.repo / "docs/report.md").unlink()
        self.git("checkout", "--orphan", "late-orphan")
        self.git("rm", "-rf", ".")
        (self.repo / "docs").mkdir(exist_ok=True)
        orphan = text.replace("spec.evil.0001", "spec.orphan.0001")
        orphan_fp = ship_docs.derive(dict(content=orphan), DERIVE)["lifecycle"]["fingerprint"]
        (self.repo / "docs/orphan.md").write_text(orphan)
        self.git("add", "-A")
        self.git("commit", "-qm", "orphan old-style identity")
        self.git("checkout", "adopted")
        self.git("merge", "--allow-unrelated-histories", "--no-ff", "-m", "merge orphan", "late-orphan")
        self.write_report("spec.orphan.0001", orphan_fp)
        self.assertIn("lacks exact pre-namespace historical proof", self.check(code=1).stderr)


if __name__ == "__main__":
    unittest.main()
