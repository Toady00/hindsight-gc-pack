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
    if command == "list":
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
    if args[2:4] == ["document", "get"]:
        result = config["document"]  # An unexpected bump read must fail.
    elif args[2:4] == ["bank", "config"]:
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
    if path == "/openapi.json":
        result = {"components": {"schemas": {"RetainRequest": {"properties": {"operation_id": {}, "async": {"type": "boolean"}}}}}}
    elif path.endswith("/operations"):
        result = {"operations": [], "total": 0}
    elif path.endswith("/memories"):
        payload = entry["payload"]
        op = payload["operation_id"]
        server["operations"][op] = {"operation_id": op, "status": config.get("operation_status", "completed")}
        # Match streaming retain: metadata can commit even when extraction fails.
        server["documents"] = [dict(id=i["document_id"], content=i["content"], tags=i["tags"],
                                    document_metadata=i["metadata"]) for i in payload["items"]]
        result = {"operation_id": op}
    elif path.endswith("/retry"):
        op = path.split("/")[-2]
        server["operations"][op]["status"] = "completed"
        result = {"operation_id": op, "success": True}
    elif "/operations/" in path:
        result = server["operations"][path.split("/")[-1]]
    elif path.endswith("/documents"):
        query = parse_qs(url.query)
        offset, limit = int(query["offset"][0]), int(query["limit"][0])
        result = {"items": server["documents"][offset:offset + limit], "total": len(server["documents"])}
    else:
        raise AssertionError(args)
    (root / "server.json").write_text(json.dumps(server))
else:
    raise AssertionError((tool, args))
print(json.dumps(result))
