"""Deterministic provider and fail-closed Hindsight boundary for runtime tests."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback


ROOT = Path(os.environ["HS_RUNTIME_ROOT"])
with (ROOT / "processes.jsonl").open("a") as stream:
    stream.write(json.dumps(dict(pid=os.getpid())) + "\n")


def log(event, **values):
    with (ROOT / "worker.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(event=event, **values)) + "\n")


def read_bead(bead_id):
    result = subprocess.run(["gc", "bd", "show", bead_id, "--json"], check=True,
                            capture_output=True, text=True, timeout=20)
    return json.loads(result.stdout)[0]


def boundary(name, args):
    with (ROOT / "boundary.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(name=name, args=args)) + "\n")
    if name == "hindsight":
        assert args[:4] == ["-o", "json", "mental-model", "get"], args
        fault = (ROOT / "audit-fault").read_text()
        if fault == "read-error":
            print("fixture model read unavailable", file=sys.stderr)
            return 5
        if fault == "malformed-json":
            print("fixture invalid JSON {")
            return 0
        print(json.dumps({"content": "fixture model: no unsupported claims"}))
        return 0
    if name not in ("ship-docs.sh", "bank-maintain.sh"):
        raise RuntimeError(f"Forbidden external command: {name} {args}")
    assert os.environ["HINDSIGHT_WRITER"] == "archivist"
    assert os.environ["GC_SESSION_ID"]
    if name == "ship-docs.sh" and (ROOT / "reset-shipping").exists():
        return reset_shipping()
    code = int((ROOT / "exit-code").read_text())
    print(f"fixture {name}: exit={code}; drain/consolidate/audit_findings")
    return code


def reset_shipping():
    """Real receipt/report/lock code; only the Hindsight API is synthetic."""
    sys.path.insert(0, str(ROOT / "pack/assets/scripts"))
    from ingestion import BeadsStore, Ingestor, ship_lock
    from ship_report import ScanReport

    class API:
        def capabilities(self):
            pass

        def request(self, method, suffix, payload=None):
            assert method == "POST" and suffix == "memories"
            with (ROOT / "retain-posts.jsonl").open("a") as stream:
                stream.write(json.dumps(payload) + "\n")
            return dict(operation_id=payload["operation_id"])

        def operation(self, op_id):
            return dict(operation_id=op_id, status="completed", result_metadata=dict(extraction_errors_count=0))

    store = BeadsStore(os.environ["HINDSIGHT_API"], os.environ["HINDSIGHT_BANK"])
    with ship_lock(store):
        work = store.current_work()
        assert work
        scan = ScanReport(store, True, work_id=work)
        scan.run["roots"] = [dict(status="ok", root="fixture/docs")]
        scan.start()
        if not (ROOT / "reset-ready").exists():
            Ingestor(store, API(), work_id=work).retain(
                dict(document_id="doc-a", content="English fixture", tags=["scope:repo"]),
                "fixture-hash", dict(kind="git", repository="fixture", relpath="docs/a.md"))
            (ROOT / "reset-ready").write_text(work)
            log("receipt", bead_id=work)
            parent = os.getppid()
            deadline = time.monotonic() + 30
            while os.getppid() == parent and time.monotonic() < deadline:
                time.sleep(.05)
            return 3
        scan.run.update(counts=dict(shipped=0), error="fixture bank drain timeout")
        scan.finish(3)
        print("fixture bank drain timeout; prior document receipt preserved")
        return 3


def worker():
    # gc prepends its executable directory to runtime PATH. On this machine that
    # directory also contains the real Hindsight CLI; restore the isolated tools.
    os.environ["PATH"] = f"{ROOT / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin"
    assert Path(os.environ["HOME"]) == ROOT / "home"
    assert os.environ.get("HINDSIGHT_API_KEY", "") == ""
    session = os.environ["GC_SESSION_ID"]
    origin = os.environ.get("GC_SESSION_ORIGIN") or read_bead(session)["metadata"].get("session_origin")
    log("wake", session_id=session, pid=os.getpid(), spawn_origin=os.environ.get("GC_SPAWN_ORIGIN", ""),
        session_origin=origin)
    deadline = time.monotonic() + 30
    while not (ROOT / "claim-allowed").exists():
        assert time.monotonic() < deadline, "parent never observed Ready"
        time.sleep(.05)
    while execute_claim(session):
        pass
    # No external nudge is needed to claim the next task in the same queue.
    while True:
        time.sleep(1)


def execute_claim(session):
    result = subprocess.run(["gc", "hook", "--claim", "--json"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, (result.stdout, result.stderr,
        {k: v for k, v in os.environ.items() if k.startswith("GC_")},
        subprocess.run(["gc", "ready", "--json"], capture_output=True, text=True, timeout=20).stdout)
    claim = json.loads(result.stdout) if result.stdout.strip() else {}
    if not claim.get("bead_id"):
        return False
    bead_id = claim["bead_id"]
    bead = read_bead(bead_id)
    assert bead["status"] == "in_progress", bead
    owners = {session}
    if read_bead(session)["metadata"].get("session_origin") == "named":
        owners.add("hindsight.archivist")
    assert bead["assignee"] in owners, bead
    log("claim", bead_id=bead_id, result=claim)
    if (ROOT / "queue-test").exists():
        time.sleep(11)  # Let the controller record each claim before it changes.
    description = bead["description"]
    phases = re.split(r"(?m)^## \d+\. ", description)[1:]
    assert len(phases) in (2, 3), description
    commands = re.findall(r"(?m)^ {4}(gc hindsight (?:ship|maintain).*?)$", phases[0])
    assert len(commands) == 1, phases[0]
    log("execute", command=commands[0])
    result = subprocess.run(["bash", "-eu", "-c", commands[0]],
                            capture_output=True, text=True, timeout=60)
    audit = "skipped"
    audit_errors = []
    outcome = "clean" if result.returncode == 0 else "blocked"
    audit_codes = []
    if len(phases) == 3:
        gate = re.search(r"Skip this phase unless maintain exited ([\d or]+) \(", phases[1])
        assert gate, "missing model audit exit-code instructions"
        audit_codes = [int(code) for code in re.findall(r"\d+", gate.group(1))]
        branches = dict(re.findall(r"(?ms)^- (\d+): (.*?)(?=^- \d+: |\Z)", phases[0]))
        branch = branches.get(str(result.returncode))
        assert branch, "missing maintenance exit-code instructions"
        if "tag audit has findings" in branch:
            outcome = "findings"
    if len(phases) == 3 and result.returncode in audit_codes:
        command = re.search(r"`(gc hindsight read .*?)`", phases[1]).group(1)
        models = re.search(r"For each id in\s+(.*?): fetch it", phases[1], re.S).group(1).split()
        scratch = ROOT / "models"
        scratch.mkdir(exist_ok=True)
        env = dict(os.environ, TMP=str(scratch))
        audit = "clean"
        for model in models:
            assert re.fullmatch(r"[a-z0-9-]+", model), model
            rendered = command.replace("<id>", model)
            try:
                fetched = subprocess.run(["bash", "-eu", "-c", rendered], env=env,
                                         capture_output=True, text=True, timeout=20)
                if fetched.returncode:
                    raise ValueError(f"read exit {fetched.returncode}: {fetched.stderr.strip()}")
                content = json.loads((scratch / f"{model}.json").read_text())["content"]
                if not isinstance(content, str):
                    raise ValueError("model content is not text")
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
                diagnostic = dict(model=model, command=rendered, error=f"{type(exc).__name__}: {exc}")
                audit_errors.append(diagnostic)
                audit, outcome = "incomplete", "blocked"
                log("audit_failed", **diagnostic)
                break
            assert "fixture model" in content
            log("audit", model=model, command=rendered)
    completion = " ".join(phases[-1].split())
    report_instruction = "Write the report to this root's notes before closing this root."
    assert report_instruction in completion
    assert "Close as the claimed root's confirmed assignee using --actor, never --force." in completion
    incomplete = re.search(
        r"^\s*If (.+?) was incomplete, record that explicitly in both notes and the close reason;",
        completion.split(report_instruction, 1)[1])
    assert incomplete, "missing incomplete-work notes and close-reason instructions"
    reason = (f"Incomplete: {incomplete.group(1)}; exit {result.returncode}" if outcome == "blocked" else
              f"Completed with {outcome}; exit {result.returncode}")
    if audit_errors:
        reason += f"; model audit incomplete: {audit_errors[0]['model']}: {audit_errors[0]['error']}"
    report = dict(exit_code=result.returncode, output=result.stdout + result.stderr,
                  outcome=outcome, model_audit=audit, audit_errors=audit_errors,
                   incomplete=outcome == "blocked", close_reason=reason)
    if (ROOT / "reset-shipping").exists() and len(phases) == 2:
        evidence = subprocess.run(["gc", "hindsight", "status", "--task", bead_id],
                                  capture_output=True, text=True, timeout=20)
        assert evidence.returncode in (0, 1), evidence.stderr
        report["receipt_report"] = json.loads(evidence.stdout)
    subprocess.run(["gc", "bd", "update", bead_id, "--notes", json.dumps(report)], check=True, timeout=20)
    reported = read_bead(bead_id)
    assert reported["notes"] == json.dumps(report)
    assert reported["status"] == "in_progress"
    log("report", **report)
    closed = subprocess.run(["gc", "bd", "close", bead_id, "--actor", bead["assignee"], "--reason", reason],
                            capture_output=True, text=True, timeout=20)
    assert closed.returncode == 0, (closed.stdout, closed.stderr)
    completed = read_bead(bead_id)
    assert completed["status"] == "closed"
    assert completed["close_reason"] == reason
    log("closed", bead_id=bead_id, reason=reason)
    assert "After closing, run gc hook --claim --json" in description
    return True


if __name__ == "__main__":
    if sys.argv[1] == "boundary":
        sys.exit(boundary(Path(sys.argv[2]).name, sys.argv[3:]))
    try:
        worker()
    except Exception:
        log("error", traceback=traceback.format_exc())
        # Avoid a crash/restart storm while the parent collects diagnostics.
        time.sleep(60)
        raise
