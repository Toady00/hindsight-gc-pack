"""Durable ingestion receipts. The caller must drain the bank before recovery.

The managed archivist is the single writer. Pinned beads provide durability,
not a distributed lock. No successful document hash is inferred from inventory.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import quote, urlsplit
import uuid


class Error(Exception):
    def __init__(self, message, code=5):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require_writer():
    if os.environ.get("HINDSIGHT_WRITER") != "archivist" or not os.environ.get("GC_SESSION_ID"):
        raise Error("writes require HINDSIGHT_WRITER=archivist and GC_SESSION_ID", 2)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError):
        raise Error("ingestion data must be valid JSON") from None


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _connection(api, bank):
    if not isinstance(api, str) or not isinstance(bank, str):
        raise Error("API URL and bank must be strings", 2)
    try:
        parts = urlsplit(api)
    except ValueError:
        raise Error("invalid API URL", 2) from None
    if (parts.scheme not in ("http", "https") or not parts.netloc or parts.username
            or parts.password or parts.query or parts.fragment or not bank):
        raise Error("invalid API URL or bank; credentials must not be in URLs", 2)
    return api.rstrip("/"), bank


@contextmanager
def _temporary_json(value):
    try:
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", suffix=".json") as file:
            file.write(_json(value))
            file.flush()
            yield file.name
    except OSError:
        raise Error("cannot use temporary request file") from None


class BeadsStore:
    VERSION = 1
    TYPES = {"document": "hindsight-document", "bank": "hindsight-bank"}

    def __init__(self, api, bank):
        self.api, self.bank = _connection(api, bank)
        self.city = os.environ.get("GC_CITY_PATH", "")
        if not Path(self.city).is_absolute():
            raise Error("GC_CITY_PATH must be an absolute city path", 2)
        self.command = [os.environ.get("GC_BIN") or "gc", "bd", "--city", self.city]
        self._types_ready = False

    def _call(self, *args):
        try:
            result = subprocess.run(self.command + list(args), capture_output=True, text=True, timeout=120)
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise Error("Beads command unavailable or timed out") from None
        if result.returncode:
            # Tool errors can echo metadata or environment credentials. Never relay them.
            raise Error(f"Beads command failed: {args[0]} exited {result.returncode}; durable state is unconfirmed")
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise Error("Beads returned invalid JSON") from None

    def _identity(self, kind, document_id):
        if not isinstance(kind, str) or kind not in self.TYPES or not isinstance(document_id, str):
            raise Error("invalid ingestion record identity", 2)
        if (kind == "document" and not document_id) or (kind == "bank" and document_id):
            raise Error("invalid document ID for ingestion record kind", 2)
        return {"api": self.api, "bank": self.bank, "kind": kind, "document_id": document_id}

    def _key(self, kind, document_id):
        identity = self._identity(kind, document_id)
        return "hindsight:key:" + _hash([identity[k] for k in ("api", "bank", "kind", "document_id")])

    def _list(self, label):
        rows = self._call("list", "--all", "--limit", "0", "--label", label, "--json")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise Error("Beads list must return a bare array of records")
        return rows

    def _validate(self, row, kind, document_id):
        metadata = row.get("metadata")
        record = metadata.get("hindsight") if isinstance(metadata, dict) else None
        identity = self._identity(kind, document_id)
        if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
                or record["schema_version"] != self.VERSION
                or any(record.get(k) != v for k, v in identity.items())
                or not isinstance(record.get("data"), dict)):
            raise Error("ingestion record identity or schema mismatch")
        labels = row.get("labels", [])
        if (not isinstance(labels, list) or any(not isinstance(x, str) for x in labels)
                or "hindsight:records" not in labels or self._key(kind, document_id) not in labels
                or any(x.startswith(("gc:", "gc.", "gt:", "mol:"))
                       or x in ("template", "molecule", "agent", "role", "rig", "convoy") for x in labels)
                or row.get("status") != "pinned" or row.get("assignee")
                or metadata.get("gc.routed_to") or row.get("gc.routed_to")
                or any(row.get(k) for k in ("ephemeral", "no_history", "is_template", "wisp_plane"))
                or row.get("issue_type") != self.TYPES[kind]
                or not isinstance(row.get("id"), str) or not row["id"]):
            raise Error("ingestion record must be persistent, pinned, unassigned and unrouted")
        data = deepcopy(record["data"])
        if "document_id" in data and data["document_id"] != document_id:
            raise Error("ingestion data document ID mismatch")
        data["document_id"] = document_id
        return data

    def _find(self, kind, document_id):
        rows = self._list(self._key(kind, document_id))
        if len(rows) > 1:
            raise Error("duplicate ingestion record key; manual repair required")
        if not rows:
            return None, None
        return rows[0], self._validate(rows[0], kind, document_id)

    def get(self, kind, document_id=""):
        return self._find(kind, document_id)[1]

    def list_documents(self):
        documents, seen = [], set()
        for row in self._list("hindsight:records"):
            metadata = row.get("metadata")
            record = metadata.get("hindsight") if isinstance(metadata, dict) else None
            if not isinstance(record, dict):
                raise Error("invalid ingestion record metadata")
            if record.get("api") != self.api or record.get("bank") != self.bank:
                continue
            kind, document_id = record.get("kind"), record.get("document_id")
            data = self._validate(row, kind, document_id)
            key = (kind, document_id)
            if key in seen:
                raise Error("duplicate ingestion record key; manual repair required")
            seen.add(key)
            if kind == "document":
                documents.append(data)
        return documents

    def _register_types(self):
        if self._types_ready:
            return
        config = self._call("config", "get", "types.custom", "--json")
        if not isinstance(config, dict) or not isinstance(config.get("value"), str):
            raise Error("invalid Beads types.custom response")
        value = config["value"]
        existing = {x.strip() for x in value.split(",")}
        missing = [x for x in self.TYPES.values() if x not in existing]
        if missing:
            self._call("config", "set", "types.custom", ",".join(([value] if value else []) + missing), "--json")
        self._types_ready = True

    def put(self, kind, document_id, data):
        """Return the bead ID only after reading back and validating the write."""
        require_writer()
        row, _ = self._find(kind, document_id)
        identity = self._identity(kind, document_id)
        if not isinstance(data, dict) or data.get("document_id", document_id) != document_id:
            raise Error("invalid ingestion state", 2)
        data = deepcopy(data)
        data.pop("bead_id", None)
        data["document_id"] = document_id
        # Beads --metadata merges top-level keys. Send only our key so unrelated
        # metadata edits cannot be overwritten by a stale read.
        metadata = {"hindsight": dict(identity, schema_version=self.VERSION, data=data)}
        metadata_json = _json(metadata)
        self._register_types()
        if row:
            self._call("update", row["id"], "--metadata", metadata_json, "--json")
        else:
            title = "Hindsight document: " + document_id if kind == "document" else "Hindsight bank: " + self.bank
            self._call("create", "--title", title,
                       "--status", "pinned", "--type", self.TYPES[kind],
                       "--label", "hindsight:records," + self._key(kind, document_id),
                       "--metadata", metadata_json, "--json")
        confirmed, stored = self._find(kind, document_id)
        if stored != data or (row and confirmed["id"] != row["id"]):
            raise Error("Beads write read-back did not match; no external write is safe")
        return confirmed["id"]


@contextmanager
def _budget(api, timeout):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
        raise Error("timeout must be finite and nonnegative", 2)
    deadline = time.monotonic() + timeout
    previous = getattr(api, "deadline", None)
    api.deadline = min(previous, deadline) if previous is not None else deadline
    try:
        yield api.deadline
    finally:
        api.deadline = previous


def _pause(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Error("active operation timeout; durable attempt retained", 3)
    time.sleep(min(5, remaining))


class API:
    STATUSES = {"pending", "processing", "completed", "failed", "cancelled", "not_found"}

    def __init__(self, api, bank):
        self.api, self.bank = _connection(api, bank)
        self.deadline = None
        self._capable = False

    def request(self, method, suffix, payload=None, *, global_=False):
        url = self.api + ("/" if global_ else "/v1/default/banks/" + quote(self.bank, safe="") + "/")
        url += suffix.lstrip("/")
        maximum = 120 if self.deadline is None else min(120, self.deadline - time.monotonic())
        if maximum <= 0:
            raise Error("active operation timeout; durable attempt retained", 3)
        key = os.environ.get("HINDSIGHT_API_KEY", "")
        header = "header = " + json.dumps("Authorization: Bearer " + key) if key else ""
        args = ["curl", "--config", "-", "--silent", "--show-error", "--fail-with-body",
                "--connect-timeout", "15", "--max-time", str(maximum), "--request", method, url]

        def send(body=None):
            command = args + (["--header", "Content-Type: application/json", "--data-binary", "@" + body] if body else [])
            try:
                response = subprocess.run(command, input=header + "\n", capture_output=True,
                                          text=True, timeout=maximum + 1)
            except subprocess.TimeoutExpired:
                raise Error("Hindsight request timed out; outcome unknown",
                            3 if self.deadline is not None and time.monotonic() >= self.deadline else 5) from None
            except (OSError, UnicodeError):
                raise Error("cannot execute Hindsight HTTP client") from None
            if response.returncode:
                code = 3 if self.deadline is not None and time.monotonic() >= self.deadline else 5
                raise Error("Hindsight HTTP request failed; outcome unknown", code)
            try:
                value = json.loads(response.stdout)
            except (ValueError, TypeError):
                raise Error("Hindsight returned invalid JSON") from None
            if not isinstance(value, dict):
                raise Error("Hindsight response must be a JSON object")
            return value

        if payload is None:
            return send()
        with _temporary_json(payload) as path:
            return send(path)

    def capabilities(self):
        if not self._capable:
            spec = self.request("GET", "openapi.json", global_=True)
            try:
                properties = spec["components"]["schemas"]["RetainRequest"]["properties"]
                supported = isinstance(properties["operation_id"], dict) and properties["async"]["type"] == "boolean"
            except (KeyError, TypeError):
                supported = False
            if not supported:
                raise Error("server does not advertise client operation_id and async retain; refusing unsafe write", 2)
            self._capable = True

    def operation(self, op_id):
        result = self.request("GET", "operations/" + quote(op_id, safe=""))
        if (not isinstance(result.get("status"), str) or result["status"] not in self.STATUSES
                or result.get("operation_id") != op_id):
            raise Error("invalid Hindsight operation response")
        return result

    def drain(self, timeout):
        with _budget(self, timeout) as deadline:
            while True:
                total = 0
                for status in ("pending", "processing"):
                    result = self.request("GET", "operations?status=" + status + "&limit=1")
                    count, rows = result.get("total"), result.get("operations")
                    if (type(count) is not int or count < 0 or not isinstance(rows, list)
                            or len(rows) > 1 or len(rows) > count or (count > 0 and not rows)
                            or any(not isinstance(row, dict) or row.get("status") != status for row in rows)):
                        raise Error("invalid filtered Hindsight operation list")
                    total += count
                if not total:
                    return
                _pause(deadline)

    def inventory(self):
        documents, seen, expected = [], set(), None
        while True:
            page = self.request("GET", "documents?limit=100&offset=" + str(len(documents)))
            rows, total = page.get("items"), page.get("total")
            if (not isinstance(rows, list) or type(total) is not int or total < 0
                    or (expected is not None and total != expected)):
                raise Error("invalid or changing Hindsight document inventory")
            expected = total
            for row in rows:
                doc_id = row.get("id") if isinstance(row, dict) else None
                if not isinstance(doc_id, str) or not doc_id or doc_id in seen:
                    raise Error("invalid or duplicate Hindsight document ID")
                seen.add(doc_id)
                documents.append(row)
            if len(documents) > total or (not rows and len(documents) < total):
                raise Error("incomplete Hindsight document inventory")
            if len(documents) == total:
                return documents


def _payload_hash(item):
    normalized = deepcopy(item)
    # Source hashes already cover authored timestamps. A schema's default "now"
    # must not turn an unchanged source into a fresh ingestion on every scan.
    normalized.pop("timestamp", None)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        for key in ("source_commit", "source_ref", "ref"):
            metadata.pop(key, None)
    return _hash(normalized)


def _extraction_errors(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "extraction_errors_count":
                if type(child) is not int or child < 0:
                    raise Error("invalid extraction_errors_count; completion unconfirmed")
                if child > 0:
                    return True
            if _extraction_errors(child):
                return True
    elif isinstance(value, list):
        return any(_extraction_errors(child) for child in value)
    return False


def _children(result):
    children = result.get("child_operations")
    # The HTTP model also emits null for ordinary, non-parent operations.
    if children is None:
        return None
    if not isinstance(children, list):
        raise Error("invalid child operation list; completion unconfirmed")
    seen = set()
    for child in children:
        if (not isinstance(child, dict) or not isinstance(child.get("operation_id"), str)
                or not isinstance(child.get("status"), str) or child["status"] not in API.STATUSES):
            raise Error("invalid child operation summary; completion unconfirmed")
        try:
            child_id = uuid.UUID(child["operation_id"])
        except ValueError:
            raise Error("invalid child operation ID; completion unconfirmed") from None
        if child_id in seen:
            raise Error("duplicate child operation; completion unconfirmed")
        seen.add(child_id)
    return children


class Ingestor:
    def __init__(self, store, api):
        self.store, self.api = store, api

    def _save(self, document_id, state):
        # The store owns write confirmation; a second read adds no guarantee.
        self.store.put("document", document_id, state)

    def _submit(self, document_id, state):
        self.api.capabilities()
        self._save(document_id, state)
        attempt = state["attempt"]
        response = self.api.request("POST", "memories", attempt["payload"])
        if response.get("operation_id") != attempt["operation_id"]:
            raise Error("retain acknowledgement operation ID mismatch; original intent retained")
        attempt["state"] = "pending"
        self._save(document_id, state)

    def _verify_completed(self, result, deadline):
        seen = {uuid.UUID(result["operation_id"])}
        pending = [result]
        while pending:
            if time.monotonic() >= deadline:
                raise Error("active operation timeout; child completion unconfirmed", 3)
            operation = pending.pop()
            if isinstance(operation, str):
                op_id = operation
                operation = self.api.operation(op_id)
                if not isinstance(operation, dict) or operation.get("operation_id") != op_id:
                    raise Error("invalid child operation response; completion unconfirmed")
            status = operation.get("status")
            if status != "completed":
                if status in ("failed", "cancelled"):
                    raise Error("child extraction operation failed or cancelled; manual inspection required", 1)
                raise Error("child operation is not completed or is missing; manual inspection required")
            if _extraction_errors(operation):
                raise Error("operation completed with extraction errors; manual inspection required", 1)
            children = _children(operation)
            if not children and operation.get("operation_type") == "batch_retain":
                raise Error("batch parent has no verifiable children; manual inspection required")
            # Parent summaries omit extraction counts. Fetch every descendant's
            # full status, rather than treating its summary as a receipt.
            for child in children or []:
                child_id = uuid.UUID(child["operation_id"])
                if child_id in seen:
                    raise Error("duplicate or cyclic child operation; manual inspection required")
                seen.add(child_id)
                pending.append(child["operation_id"])
        if time.monotonic() >= deadline:
            raise Error("active operation timeout; child completion unconfirmed", 3)

    def _finish(self, document_id, state, deadline, retry):
        attempt = state["attempt"]
        phase = attempt.get("phase")
        if phase == "reprocess_prepared":
            raise Error("reprocess acknowledgement unknown; inspect Hindsight operation list and repair bead "
                        "with known reprocess_operation_id and phase=reprocess_wait; never blindly reprocess")
        if phase not in ("retain", "reprocess_wait"):
            raise Error("unknown ingestion phase; manual inspection required")
        payload = attempt.get("payload")
        if (attempt.get("state") not in ("prepared", "pending", "unknown", "failed")
                or not isinstance(attempt.get("source_hash"), str) or not attempt["source_hash"]
                or "source" not in attempt or type(attempt.get("reprocess")) is not bool
                or not isinstance(payload, dict) or payload.get("operation_id") != attempt.get("operation_id")
                or payload.get("async") is not True or not isinstance(payload.get("items"), list)
                or len(payload["items"]) != 1 or not isinstance(payload["items"][0], dict)
                or payload["items"][0].get("document_id") != document_id
                or _payload_hash(payload["items"][0]) != attempt.get("payload_hash")):
            raise Error("missing or inconsistent durable attempt; manual inspection required")
        replayed = False
        while True:
            if time.monotonic() >= deadline:
                raise Error("active operation timeout; durable attempt retained", 3)
            reprocessing = attempt["phase"] == "reprocess_wait"
            op_id = attempt.get("reprocess_operation_id") if reprocessing else attempt.get("operation_id")
            try:
                uuid.UUID(op_id)
            except (ValueError, TypeError, AttributeError):
                raise Error("invalid persisted operation ID; manual inspection required") from None
            result = self.api.operation(op_id)
            status = result.get("status")
            if status == "not_found":
                if reprocessing:
                    raise Error("reprocess operation not found; manual inspection required")
                if replayed:
                    raise Error("replayed retain operation still not found; outcome unknown")
                self._submit(document_id, state)
                replayed = True
                continue
            if status in ("failed", "cancelled"):
                attempt["state"] = "failed"
                attempt["error"] = "Hindsight extraction operation " + op_id + " " + status
                self._save(document_id, state)
                children = _children(result)
                if (children is not None or result.get("operation_type") == "batch_retain") and not any(
                        child["status"] in ("failed", "cancelled") for child in children or []):
                    attempt["error"] = "batch parent " + op_id + " has no retryable children; manual inspection required"
                    self._save(document_id, state)
                    raise Error(attempt["error"], 1)
                if not retry:
                    raise Error(attempt["error"], 1)
                retry = False
                attempt["state"] = "pending"
                self._save(document_id, state)
                response = self.api.request("POST", "operations/" + quote(op_id, safe="") + "/retry")
                if response.get("operation_id") != op_id or response.get("success") is not True:
                    raise Error("operation retry acknowledgement invalid; original intent retained")
                continue
            if status == "completed":
                try:
                    self._verify_completed(result, deadline)
                except Error as error:
                    attempt["state"] = "failed" if error.code == 1 else "unknown"
                    attempt["error"] = "operation " + op_id + " completion unconfirmed; inspect operation and child details"
                    self._save(document_id, state)
                    raise
                if attempt.get("reprocess") and not reprocessing:
                    attempt["phase"] = "reprocess_prepared"
                    attempt["state"] = "prepared"
                    attempt["note"] = "If acknowledgement is lost, inspect Hindsight operations and repair the bead; do not POST again."
                    self._save(document_id, state)
                    response = self.api.request("POST", "documents/" + quote(document_id, safe="") + "/reprocess")
                    reprocess_id = response.get("operation_id")
                    try:
                        uuid.UUID(reprocess_id)
                    except (ValueError, TypeError, AttributeError):
                        raise Error("reprocess acknowledgement invalid; manual inspection required") from None
                    if response.get("success") is not True:
                        raise Error("reprocess acknowledgement invalid; manual inspection required")
                    attempt["reprocess_operation_id"] = reprocess_id
                    attempt["phase"] = "reprocess_wait"
                    attempt["state"] = "pending"
                    self._save(document_id, state)
                    retry = False
                    continue
                completed_at = now()
                state["last_success"] = {"source_hash": attempt["source_hash"],
                                         "payload_hash": attempt["payload_hash"],
                                         "operation_id": attempt["operation_id"], "completed_at": completed_at}
                if reprocessing:
                    state["last_success"]["reprocess_operation_id"] = op_id
                state["source"] = deepcopy(attempt["source"])
                attempt["state"] = "succeeded"
                attempt["completed_at"] = completed_at
                attempt.pop("payload", None)
                attempt.pop("error", None)
                attempt.pop("note", None)
                self._save(document_id, state)
                return
            if status not in ("pending", "processing"):
                raise Error("unknown Hindsight operation status; attempt retained")
            _pause(deadline)

    def recover(self, document_id, timeout=900):
        state = self.store.get("document", document_id)
        if not state or state.get("attempt") is None:
            return False
        if not isinstance(state["attempt"], dict):
            raise Error("invalid ingestion attempt")
        if state["attempt"].get("state") == "succeeded":
            return False
        require_writer()
        with _budget(self.api, timeout) as deadline:
            self._finish(document_id, state, deadline, retry=True)
        return True

    def retain(self, item, source_hash, source, bank_hash=None, force=False, timeout=900):
        require_writer()
        document_id = item.get("document_id") if isinstance(item, dict) else None
        if not isinstance(document_id, str) or not document_id or not isinstance(source_hash, str) or not source_hash:
            raise Error("retain needs a document_id and source_hash", 2)
        payload_hash = _payload_hash(item)
        with _budget(self.api, timeout) as deadline:
            previous = self.store.get("document", document_id) or {}
            previous_attempt = previous.get("attempt")
            recovered_reprocess = isinstance(previous_attempt, dict) and previous_attempt.get("reprocess") is True
            recovered = self.recover(document_id, timeout=max(0, deadline - time.monotonic()))
            state = self.store.get("document", document_id) or {"document_id": document_id}
            receipt = state.get("last_success") or {}
            matches = receipt.get("source_hash") == source_hash and receipt.get("payload_hash") == payload_hash
            if recovered and matches and (not force or recovered_reprocess):
                # A resumed force attempt already performed its requested reprocess.
                return "shipped"
            if not force and matches and bank_hash == source_hash:
                return "unchanged"
            self.api.capabilities()
            op_id = str(uuid.uuid4())
            state["source"] = deepcopy(source)
            state["attempt"] = {"source_hash": source_hash, "payload_hash": payload_hash,
                                "operation_id": op_id, "state": "prepared", "phase": "retain",
                                "payload": {"items": [deepcopy(item)], "async": True, "operation_id": op_id},
                                "source": deepcopy(source), "reprocess": bool(force), "prepared_at": now()}
            self._submit(document_id, state)
            self._finish(document_id, state, deadline, retry=False)
            return "shipped"
