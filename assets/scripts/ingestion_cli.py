"""Agent-memory ingestion adapter. The caller must drain before recovery."""

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
from connection import resolve
from ingestion import API, BeadsStore, Error, Ingestor, _hash, require_writer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    recover = commands.add_parser("recover")
    recover.add_argument("bank")
    recover.add_argument("document_id")
    retain = commands.add_parser("retain")
    retain.add_argument("bank")
    args = parser.parse_args(argv)
    try:
        require_writer()
        try:
            connection = resolve()
        except (ValueError, OSError):
            raise Error("invalid or missing Hindsight connection configuration", 2) from None
        os.environ.update(HINDSIGHT_API=connection["api"], HINDSIGHT_API_URL=connection["api"],
                          HINDSIGHT_API_KEY=connection["key"])
        store = BeadsStore(connection["api"], args.bank)
        ingestor = Ingestor(store, API(connection["api"], args.bank))
        if args.command == "recover":
            if not args.document_id:
                raise Error("recover needs a document_id", 2)
            print("recovered" if ingestor.recover(args.document_id) else "idle")
        else:
            try:
                payload = json.load(sys.stdin)
            except (ValueError, OSError, UnicodeError):
                raise Error("retain stdin must be valid JSON", 2) from None
            if (not isinstance(payload, dict) or payload.get("async") is not True
                    or not isinstance(payload.get("items"), list) or len(payload["items"]) != 1
                    or not isinstance(payload["items"][0], dict)):
                raise Error("retain needs one item and async=true", 2)
            item = payload["items"][0]
            # Hash every field, including the report timestamp, without changing the item.
            ingestor.retain(item, source_hash=_hash(item), source={"kind": "agent-memory"}, bank_hash=None)
            receipt = store.get("document", item["document_id"])["last_success"]
            print(f"RETAINED {item['document_id']} op={receipt['operation_id']}")
        return 0
    except Error as error:
        print(f"ingestion: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    sys.exit(main())
