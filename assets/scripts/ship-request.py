#!/usr/bin/env python3
"""Transport ship arguments through a formula without evaluating shell text."""
import base64
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from connection import resolve

VALUE_FLAGS = {"--bank", "--api", "--ref", "--domains", "--schema", "--drain-timeout"}
SWITCHES = {"--fetch", "--reprocess"}
PATH_FLAGS = {"--domains", "--schema"}


def normalize(args):
    result = []
    roots = False
    while args:
        arg, *args = args
        if arg == "--":
            roots = True
        elif not roots and arg in VALUE_FLAGS:
            if not args:
                raise ValueError(f"missing value for {arg}")
            value, *args = args
            if arg == "--api":
                value = resolve(value)["api"]
            result.extend([arg, str(Path(value).absolute()) if arg in PATH_FLAGS else value])
        elif not roots and arg in SWITCHES:
            result.append(arg)
        elif arg.startswith("-") and not roots:
            raise ValueError(f"unsupported flag {arg}")
        else:
            result.append(str(Path(arg).absolute()))
    return result


def main():
    mode = sys.argv[1]
    if mode == "encode":
        args = normalize(sys.argv[2:])
        for flag, names in [("--bank", ["HINDSIGHT_BANK"]), ("--api", ["HINDSIGHT_API", "HINDSIGHT_API_URL"]), ("--schema", ["HINDSIGHT_SCHEMA"])]:
            if flag not in args:
                value = next((os.environ[n] for n in names if os.environ.get(n)), "")
                if value:
                    if flag == "--api":
                        value = resolve(value)["api"]
                    args = [flag, str(Path(value).absolute()) if flag in PATH_FLAGS else value] + args
        print(base64.b64encode(json.dumps(args).encode()).decode())
    elif mode == "execute":
        if os.environ.get("HINDSIGHT_WRITER") != "archivist" or not os.environ.get("GC_SESSION_ID"):
            raise ValueError("ship requests execute only in the managed archivist")
        args = json.loads(base64.b64decode(sys.argv[2], validate=True))
        if not isinstance(args, list) or not all(isinstance(a, str) and "\0" not in a for a in args):
            raise ValueError("request must be a string argument array")
        # Revalidate the allowlist. Explicit paths were made absolute at enqueue.
        args = normalize(args)
        script = Path(__file__).resolve().parents[2] / "commands/ship/run.sh"
        os.execv(str(script), [str(script), *args])
    else:
        raise ValueError("expected encode or execute")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"ship request: {error}", file=sys.stderr)
        sys.exit(2)
