"""A pinned bank record holds scan health across archivist machine handoffs."""
from copy import deepcopy
from datetime import datetime, timezone
import os
import uuid

from ingestion import Error, now


class ScanReport:
    def __init__(self, store, full_scan, work_id=""):
        self.store = store
        self.run = dict(run_id=str(uuid.uuid4()), started_at=now(), status="running",
                        full_scan=full_scan, roots=[], documents=[], counts={}, work_id=work_id)

    def start(self):
        data = self.store.get("bank") or {}
        previous = data.get("latest_run")
        history = data.get("recent_runs", [])
        if not isinstance(history, list) or any(not isinstance(run, dict) for run in history):
            raise Error("invalid scan history; refusing to replace evidence")
        if previous:
            if not isinstance(previous, dict):
                raise Error("invalid previous scan; refusing to replace evidence")
            # Keep bounded summaries; duplicating roots and per-document output
            # for every scan would quickly exceed Beads' inline metadata budget.
            summary = {key: deepcopy(previous[key]) for key in (
                "run_id", "work_id", "started_at", "finished_at", "status", "exit_code", "full_scan", "counts", "error")
                if key in previous}
            data["recent_runs"] = (history + [summary])[-20:]
        data["latest_run"] = deepcopy(self.run)
        if self.run["full_scan"]:
            data["latest_full_scan"] = deepcopy(self.run)
        return self.store.put("bank", "", data)

    def finish(self, code):
        data = self.store.get("bank") or {}
        if data.get("latest_run", {}).get("run_id") != self.run["run_id"]:
            raise Error("scan ownership changed; only one archivist may write this bank")
        counts = self.run["counts"]
        clean = (code == 0 and bool(self.run["roots"])
                 and all(r["status"] == "ok" for r in self.run["roots"])
                 and not any(counts.get(k, 0) for k in ("failed", "refused", "gone", "incomplete")))
        self.run.update(finished_at=now(), exit_code=code, status="ok" if clean else "incomplete")
        data["latest_run"] = deepcopy(self.run)
        if self.run["full_scan"]:
            data["latest_full_scan"] = deepcopy(self.run)
            if clean:
                data["last_success"] = deepcopy(self.run)
        return self.store.put("bank", "", data)


def status(store, work_id=""):
    data = store.get("bank") or {}
    latest = data.get("latest_full_scan", {})
    success = data.get("last_success", {})
    last_run = data.get("latest_run", {})
    if any(not isinstance(value, dict) for value in (latest, success, last_run)):
        raise Error("invalid bank scan metadata; health is unconfirmed")
    age = None
    if success:
        try:
            completed = datetime.fromisoformat(success["finished_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - completed).total_seconds()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise Error("invalid scan completion timestamp; health is unconfirmed") from None
    max_age = int(os.environ.get("HINDSIGHT_MAX_SHIP_AGE", "7200"))
    if max_age < 0:
        raise Error("HINDSIGHT_MAX_SHIP_AGE must be nonnegative", 2)
    unresolved = []
    receipts = []
    for record in store.list_documents():
        attempt, receipt = record.get("attempt"), record.get("last_success")
        if (not isinstance(attempt, dict) or attempt.get("state") != "succeeded"
                or not isinstance(receipt, dict) or receipt.get("source_hash") != attempt.get("source_hash")
                or not receipt.get("completed_at")):
            unresolved.append(record["document_id"])
        if isinstance(receipt, dict) and receipt.get("completed_at"):
            if not work_id or receipt.get("work_id") == work_id:
                receipts.append(dict(deepcopy(receipt), document_id=record["document_id"]))
    healthy = (age is not None and 0 <= age <= max_age and latest.get("status") == "ok"
               and last_run.get("status") == "ok" and not unresolved)
    history = data.get("recent_runs", [])
    if not isinstance(history, list) or any(not isinstance(run, dict) for run in history):
        raise Error("invalid scan history")
    scans = [run for run in [*history, last_run] if run and (not work_id or run.get("work_id") == work_id)]
    return dict(healthy=healthy, last_success_at=success.get("finished_at"), age_seconds=age,
                max_age_seconds=max_age, latest_full_scan=latest,
                latest_run=last_run, unresolved_documents=unresolved,
                document_receipts=receipts, work_id=work_id, recent_scans=scans,
                receipt_scope="latest confirmed receipt per document; not last-scan counts")
