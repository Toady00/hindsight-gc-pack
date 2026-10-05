"""Publication lifecycle: identity, per-document gates, report sets, and recovery."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
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

    def test_report_gate_requires_an_updated_report_in_the_same_set(self):
        spec, prd, report = "alpha.spec.a.0001", "alpha.prd.a.0001", "alpha.build-report.omg-1"
        old_spec, old_prd = candidate(spec, FP["a"], "accepted"), candidate(prd, FP["c"], "accepted")
        old_report = candidate(report, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"], prd: FP["c"]})
        published = {spec: record(old_spec), prd: record(old_prd), report: record(old_report)}
        new_spec = candidate(spec, FP["b"], "accepted")
        # Acceptance alone never unlocks the gate.
        decisions, sets = self.plan({spec: new_spec, prd: old_prd, report: old_report}, published)
        self.assertEqual(decisions[spec].action, "hold")
        self.assertIn(report, decisions[spec].reason)
        self.assertEqual(sets, [])
        new_report = candidate(report, FP["f"], kind=publication.REPORT, assesses={spec: FP["b"], prd: FP["c"]})
        decisions, sets = self.plan({spec: new_spec, prd: old_prd, report: new_report}, published)
        self.assertEqual(actions(decisions), {spec: "publish", prd: "unchanged", report: "publish"})
        self.assertEqual(len(sets), 1)
        self.assertEqual((sets[0]["withdraw"], sets[0]["documents"], sets[0]["reports"]), ([report], [spec], [report]))
        self.assertEqual(decisions[spec].set_id, decisions[report].set_id)
        # A report pinning a revision the bank will not hold cannot publish.
        draft_edit = candidate(spec, FP["d"])
        wrong = candidate(report, FP["f"], kind=publication.REPORT, assesses={spec: FP["d"], prd: FP["c"]})
        decisions, _ = self.plan({spec: draft_edit, prd: old_prd, report: wrong}, published)
        self.assertEqual((decisions[spec].action, decisions[report].action), ("hold", "hold"))

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

    def test_every_governing_report_must_move_and_retiring_a_report_releases_nothing(self):
        spec, first, second = "alpha.spec.a.0001", "alpha.build-report.one", "alpha.build-report.two"
        old_spec = candidate(spec, FP["a"], "accepted")
        reports = {r: candidate(r, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"]}, source=r.ljust(64, "x"))
                   for r in (first, second)}
        published = {spec: record(old_spec), **{r: record(c) for r, c in reports.items()}}
        new_spec = candidate(spec, FP["b"], "accepted")
        updated = candidate(first, FP["f"], kind=publication.REPORT, assesses={spec: FP["b"]})
        decisions, _ = self.plan({spec: new_spec, first: updated, second: reports[second]}, published)
        self.assertEqual((decisions[spec].action, decisions[first].action), ("hold", "hold"))
        self.assertIn(second, decisions[spec].reason)
        # Retiring the second report is a status-only change, but it still governs.
        published[second]["status"] = "deprecated"
        decisions, sets = self.plan({spec: new_spec, first: updated, second: dict(reports[second], status="deprecated")},
                                    published)
        self.assertEqual(decisions[spec].action, "hold")
        self.assertIn(second, decisions[spec].reason)
        # Both reports moving to the new revision releases it.
        second_updated = candidate(second, FP["f"], kind=publication.REPORT, assesses={spec: FP["b"]},
                                   source="z" * 64)
        published[second]["status"] = "draft"
        decisions, sets = self.plan({spec: new_spec, first: updated, second: second_updated}, published)
        self.assertEqual(actions(decisions), {spec: "publish", first: "publish", second: "publish"})
        self.assertEqual(sets[0]["withdraw"], [first, second])

    def test_an_update_that_drops_a_pin_does_not_release_it(self):
        spec, prd, report = "alpha.spec.a.0001", "alpha.prd.a.0001", "alpha.build-report.one"
        published = {spec: record(candidate(spec, FP["a"], "accepted")), prd: record(candidate(prd, FP["c"], "accepted")),
                     report: record(candidate(report, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"], prd: FP["c"]}))}
        dropped = candidate(report, FP["f"], kind=publication.REPORT, assesses={prd: FP["c"]})
        decisions, sets = self.plan({spec: candidate(spec, FP["b"], "accepted"), prd: candidate(prd, FP["c"], "accepted"),
                                     report: dropped}, published)
        self.assertEqual(decisions[spec].action, "hold")
        self.assertEqual(decisions[report].action, "publish")  # a correction of what it still pins
        self.assertEqual(sets, [])

    def test_withdrawn_report_still_governs_until_its_replacement_publishes(self):
        spec, report = "alpha.spec.a.0001", "alpha.build-report.one"
        report_record = record(candidate(report, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"]}), visible=False)
        published = {spec: record(candidate(spec, FP["b"], "accepted")), report: report_record}
        self.assertEqual(self.plan({spec: candidate(spec, FP["c"], "accepted")}, published)[0][spec].action, "hold")
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

    def test_first_publication_of_a_set_orders_documents_before_their_report(self):
        spec, report = "alpha.spec.a.0001", "alpha.build-report.one"
        decisions, sets = self.plan({
            spec: candidate(spec, FP["a"], "accepted"),
            report: candidate(report, FP["e"], kind=publication.REPORT, assesses={spec: FP["a"]})})
        self.assertEqual(actions(decisions), {spec: "publish", report: "publish"})
        self.assertEqual(sets[0], dict(set_id=sets[0]["set_id"], documents=[spec], reports=[report], withdraw=[]))


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
        self.accepted = []
        for name, value in (("BeadsStore", Mock(return_value=self.store)),
                            ("API", Mock(return_value=self.bank)),
                            ("require_writer", Mock()), ("ship_lock", Mock(side_effect=lambda s: nullcontext())),
                            ("snapshot", Mock(side_effect=self.snapshot)),
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
                    ref="refs/heads/main", commit="c" * 40, toplevel="/rig", namespace="alpha\n",
                    accepted_ids=list(self.accepted))
        documents = [dict(content=text, repo="rig", repository=REPO, relpath=path, ref=root["ref"],
                          commit=root["commit"], root=root["root"]) for path, text in sorted(self.files.items())]
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

    def test_build_iteration_publishes_report_and_specs_together(self):
        spec = self.doc("a", "accepted", "R1 v1")
        report = self.report("R1 implemented", "a")
        self.ship()
        self.assertEqual(self.bank.events, [("retain", spec), ("retain", report)])
        self.assertEqual(self.store.get("namespace", "alpha")["repository"], REPO)
        self.assertTrue(self.published(spec)["ever_accepted"])
        set_id = self.published(report)["set"]
        self.assertEqual(self.store.get("set", set_id)["state"], "published")
        # Revise after acceptance: the draft does not publish; nor does reacceptance alone.
        self.bank.events.clear()
        self.doc("a", "draft", "R1 v2")
        output = self.ship()
        self.assertIn("HELD", output)
        self.doc("a", "accepted", "R1 v2")
        self.assertIn("published build report assesses", self.ship())
        self.assertEqual(self.bank.events, [])
        # The rebuilt report pins the new revision; it withdraws, then specs, then itself.
        self.report("R1 partially implemented", "a")
        self.ship()
        self.assertEqual(self.bank.events, [("withdraw", report), ("retain", spec), ("retain", report)])
        self.assertEqual(self.published(spec)["fingerprint"], self.fingerprint("a"))
        self.assertTrue(self.published(report)["visible"])
        self.assertEqual(self.published(report)["assesses"][0]["fingerprint"], self.fingerprint("a"))

    def test_interrupted_set_under_claims_then_resumes(self):
        spec = self.doc("a", "accepted", "R1 v1")
        report = self.report("R1 implemented", "a")
        self.ship()
        self.doc("a", "accepted", "R1 v2")
        self.report("R1 partially implemented", "a")
        self.bank.fail.add(spec)
        self.bank.events.clear()
        output = self.ship(code=1)
        # The stale report is gone, the spec kept its old revision, the new report waited.
        self.assertEqual(self.bank.events, [("withdraw", report)])
        self.assertNotIn(report, self.bank.docs)
        self.assertIn("HELD", output)
        sets = self.store.list_records("set")
        self.assertEqual([s["state"] for s in sets if s["withdraw"]], ["incomplete"])
        self.assertFalse(self.published(report)["visible"])
        # Meanwhile, a further spec edit cannot slip through the withdrawn report's gate.
        self.bank.fail.clear()
        self.bank.events.clear()
        self.ship()
        # The same planned revision resumes by retrying its original operation.
        self.assertEqual(self.bank.events, [("retry", spec), ("retain", spec), ("retain", report)])
        self.assertTrue(self.published(report)["visible"])
        self.assertTrue(all(s["state"] == "published" for s in self.store.list_records("set")))

    def test_withdrawn_report_keeps_governing_after_an_interruption(self):
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
        output = self.ship(code=1)
        self.assertIn(report, output)
        self.assertIn("does not hold this document's published revision", output)
        self.assertEqual(self.bank.events, [])

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
        # The bank copy is repaired to the published revision and its report returns.
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
        self.assertEqual(self.bank.events, [])
        self.assertEqual(self.store.get("document", spec)["attempt"]["state"], "failed")
        self.assertIn("withdrawn from the bank and held", output)
        self.assertIn("does not hold this document's published revision", output)
        # Restoring the published content repairs the bank and returns the report.
        self.files["docs/a.md"] = published_spec
        output = self.ship()
        self.assertIn("ABANDONED", output)
        self.assertEqual(self.bank.events, [("retain", spec), ("retain", report)])

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

    def test_a_pin_dropped_in_an_earlier_scan_still_governs(self):
        spec = self.doc("a", "accepted", "R1 v1")
        self.doc("b", "accepted", "R2 v1")
        report = self.report("R1 and R2 implemented", "a", "b")
        self.ship()
        self.report("R2 implemented", "b")  # a correction that drops spec a
        self.ship()
        self.assertEqual(self.published(report)["governs"], ["alpha.spec.a.0001", "alpha.spec.b.0001"])
        self.doc("a", "accepted", "R1 v2")
        self.bank.events.clear()
        self.assertIn("published build report assesses", self.ship())
        self.assertEqual(self.bank.events, [])
        self.report("R1 partial, R2 implemented", "a", "b")
        self.ship()
        self.assertEqual(self.bank.events, [("withdraw", report), ("retain", spec), ("retain", report)])

    def test_dry_run_previews_without_writes(self):
        self.doc("a", "accepted", "R1")
        self.report("R1 implemented", "a")
        output = self.ship("--dry-run")
        self.assertIn("WOULD SHIP", output)
        self.assertEqual(self.store.puts, [])
        self.assertEqual(self.bank.events, [])

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


if __name__ == "__main__":
    unittest.main()
