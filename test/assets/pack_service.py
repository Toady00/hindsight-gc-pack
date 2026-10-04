"""Executable fixture: only the service calls used by shell smoke tests."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, unquote, urlsplit

root = Path(os.environ["MOCK_ROOT"])
config = json.loads((root / "config.json").read_text())
tool, args = Path(sys.argv[0]).name, sys.argv[1:]


def value(flag):
    return args[args.index(flag) + 1]


entry = dict(tool=tool, args=args)
if tool == "curl":
    entry["stdin"] = sys.stdin.read()
if "--data-binary" in args:
    entry["payload"] = json.loads(Path(value("--data-binary")[1:]).read_text())
if "--metadata" in args:
    entry["payload"] = json.loads(value("--metadata"))
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(entry) + "\n")

if tool == "gc" and args[:1] == ["bd"]:
    assert args[1:3] == ["--city", os.environ["GC_CITY_PATH"]] and "--json" in args
    db = json.loads((root / "beads.json").read_text())
    command = args[3]
    if command == "show":
        if args[4] == "fixture-session":
            result = [dict(id="fixture-session", metadata=dict(current_claim_bead_id="fixture-work"))]
        else:
            assert args[4] == "fixture-work"
            result = [dict(id="fixture-work", status="in_progress", assignee="fixture-session")]
    elif command == "list":
        assert "--all" in args and value("--limit") == "0"
        result = [r for r in db["issues"] if value("--label") in r["labels"]]
    elif command == "config":
        assert args[5] == "types.custom"
        if args[4] == "set":
            db["types"] = args[6]
        result = {"key": "types.custom", "value": db["types"]}
    elif command == "create":
        assert value("--type") in db["types"].split(",")
        result = dict(id=f"fixture-{len(db['issues']) + 1}", issue_type=value("--type"),
                      status=value("--status"), labels=value("--label").split(","),
                      metadata=entry["payload"], assignee="")
        db["issues"].append(result)
    elif command == "update":
        result = next(r for r in db["issues"] if r["id"] == args[4])
        result["metadata"].update(entry["payload"])
    else:
        raise AssertionError(args)
    if config.get("completion_write_error") and entry.get("payload", {}).get("hindsight", {}).get("data", {}).get("attempt", {}).get("state") == "succeeded":
        sys.exit(1)
    (root / "beads.json").write_text(json.dumps(db))
elif tool == "gc" and args[:2] == ["rig", "list"]:
    if config.get("rig_error"):
        sys.exit(1)
    result = {"rigs": [{"name": "repo", "path": os.environ["GC_CITY_PATH"]}]}
elif tool == "gc" and args[:1] == ["sling"]:
    if config.get("sling_error"):
        sys.exit(1)
    result = "queued fixture-bead"
elif tool == "hindsight":
    assert args[:2] == ["-o", "json"]
    if args[2:4] == ["document", "get"]:
        result = config["document"]  # An unexpected bump read must fail.
    elif args[2:4] == ["bank", "consolidate"]:
        assert "--wait" not in args
        if config.get("consolidate_error"):
            print("fixture consolidation submission error", file=sys.stderr)
            sys.exit(1)
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        count = sum(c["tool"] == "hindsight" and c["args"][2:4] == ["bank", "consolidate"] for c in calls)
        result = config.get("consolidation_response", {"operation_id": f"consolidate-op-{count}"})
    elif args[2:4] == ["bank", "consolidation-recover"]:
        if config.get("recovery_error"):
            print("fixture recovery error", file=sys.stderr)
            sys.exit(1)
        result = {"retried_count": 0}
    elif args[2:4] == ["operation", "get"]:
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        index = sum(c["tool"] == "hindsight" and c["args"][2:4] == ["operation", "get"] for c in calls) - 1
        statuses = config.get("consolidation_statuses", ["completed"])
        status = statuses[min(index, len(statuses) - 1)]
        if status == "read_error":
            print("fixture operation read error", file=sys.stderr)
            sys.exit(1)
        result = {"operation_id": args[5], "status": status}
        if status == "failed":
            result["error_message"] = "fixture consolidation failure"
    elif args[2:4] == ["bank", "config"]:
        if config.get("config_error"):
            print("fixture config error", file=sys.stderr)
            sys.exit(1)
        result = {"config": {"enable_auto_consolidation": config.get("auto", True)}}
    elif args[2:4] == ["tag", "list"]:
        tags = config.get("tags", [])
        offset, limit = int(value("--offset")), int(value("--limit"))
        result = {"items": [{"tag": t} for t in tags[offset:offset + limit]], "total": len(tags)}
    else:
        raise AssertionError(args)
elif tool == "curl":
    server = json.loads((root / "server.json").read_text())
    url = urlsplit(next(a for a in args if a.startswith("http")))
    path = unquote(url.path)
    query = parse_qs(url.query)
    method = args[args.index("--request") + 1] if "--request" in args else "GET"
    if path == "/openapi.json":
        result = {"components": {"schemas": {"RetainRequest": {"properties": {"operation_id": {}, "async": {"type": "boolean"}}}}}}
    elif path.endswith("/consolidate") and method == "POST":
        attempts_file = root / "consolidation-connect-attempts"
        attempts = int(attempts_file.read_text()) if attempts_file.exists() else 0
        attempts_file.write_text(str(attempts + 1))
        if attempts < config.get("consolidate_connect_failures", 0):
            print("connection refused", file=sys.stderr)
            sys.exit(7)
        if config.get("consolidate_error"):
            print("fixture consolidation submission error", file=sys.stderr)
            sys.exit(config.get("consolidate_error_code", 28))
        count_file = root / "consolidation-count"
        count = int(count_file.read_text()) + 1 if count_file.exists() else 1
        count_file.write_text(str(count))
        result = config.get("consolidation_response", {"operation_id": f"consolidate-op-{count}"})
    elif path.endswith("/config"):
        if config.get("config_error"):
            print("fixture config error", file=sys.stderr)
            sys.exit(7)
        result = {"config": {"enable_auto_consolidation": config.get("auto", True)}}
    elif path.endswith("/tags"):
        if config.get("tag_error"):
            print("fixture tag error", file=sys.stderr)
            sys.exit(7)
        tags = config.get("tags", [])
        offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [500])[0])
        result = {"items": [{"tag": tag} for tag in tags[offset:offset + limit]], "total": len(tags)}
    elif path.endswith("/retry"):
        op = path.split("/")[-2]
        server["operations"][op]["status"] = "completed"
        result = {"operation_id": op, "success": True}
    elif path.endswith("/operations"):
        result = {"operations": [], "total": 0}
    elif "/operations/" in path:
        operation_id = path.split("/")[-1]
        if operation_id in server["operations"]:
            result = server["operations"][operation_id]
        else:
            count_file = root / "operation-poll-count"
            index = int(count_file.read_text()) if count_file.exists() else 0
            count_file.write_text(str(index + 1))
            statuses = config.get("consolidation_statuses", ["completed"])
            status = statuses[min(index, len(statuses) - 1)]
            if status in ("read_error", "not_found", None):
                print("fixture operation read error", file=sys.stderr)
            if status == "unauthorized":
                import time
                time.sleep(config.get("operation_status_delay", 0))
                print("401", end="")
                print("fixture unauthorized status read", file=sys.stderr)
                sys.exit(22)
            if status in ("not_found", None) and "--write-out" in args:
                print("404", end="")
                sys.exit(22)
            if status == "read_error":
                sys.exit(7)
            result = {"operation_id": operation_id, "status": status}
            if status == "failed":
                result["error_message"] = "fixture consolidation failure"
    elif path.endswith("/memories"):
        payload = entry["payload"]
        op = payload["operation_id"]
        server["operations"][op] = {"operation_id": op, "status": config.get("operation_status", "completed")}
        # Match streaming retain: metadata can commit even when extraction fails.
        server["documents"] = [dict(id=i["document_id"], content=i["content"], tags=i["tags"],
                                    document_metadata=i["metadata"]) for i in payload["items"]]
        result = {"operation_id": op}
    elif path.endswith("/documents"):
        offset, limit = int(query["offset"][0]), int(query["limit"][0])
        result = {"items": server["documents"][offset:offset + limit], "total": len(server["documents"])}
    else:
        raise AssertionError(args)
    (root / "server.json").write_text(json.dumps(server))
else:
    raise AssertionError((tool, args))
payload = json.dumps(result)
if "--output" in args:
    Path(value("--output")).write_text(payload)
else:
    print(payload)
if "--write-out" in args:
    print("200", end="")
