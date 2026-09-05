#!/usr/bin/env python3
"""Resolve one endpoint and credential for both the HTTP and CLI clients."""
import json
import os
from pathlib import Path
import sys
import tomllib
from urllib.parse import urlsplit


def resolve(explicit=""):
    env = os.environ
    api = explicit or env.get("HINDSIGHT_API") or env.get("HINDSIGHT_API_URL")
    key = env.get("HINDSIGHT_API_KEY", "")
    if not api:
        profile = env.get("HINDSIGHT_PROFILE", "")
        if profile and ("/" in profile or "\\" in profile or profile in (".", "..")):
            raise ValueError("invalid HINDSIGHT_PROFILE")
        path = (Path.home() / ".hindsight/cli-profiles" / f"{profile}.toml" if profile
                else Path(env.get("HINDSIGHT_CONFIG", str(Path.home() / ".hindsight/config"))))
        if path.is_file():
            with path.open("rb") as source:
                config = tomllib.load(source)
            api = config.get("api_url", "")
            key = env.get("HINDSIGHT_API_KEY", config.get("api_key", ""))
        elif profile or env.get("HINDSIGHT_CONFIG"):
            raise ValueError("selected Hindsight configuration file is missing")
    if not isinstance(api, str) or not api:
        raise ValueError("no API: set HINDSIGHT_API_URL, HINDSIGHT_API, --api, or configure the CLI")
    url = urlsplit(api)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("API must be an HTTP(S) base URL without credentials, query, or fragment")
    if not isinstance(key, str) or any(c in key + api for c in "\r\n\0"):
        raise ValueError("invalid connection configuration")
    return {"api": api.rstrip("/"), "key": key}


if __name__ == "__main__":
    try:
        print(json.dumps(resolve(sys.argv[1] if len(sys.argv) > 1 else "")))
    except (ValueError, OSError) as error:
        # Never echo configuration contents or credentials in diagnostics.
        print(f"Hindsight connection: {type(error).__name__}: invalid or missing configuration" if isinstance(error, tomllib.TOMLDecodeError) else f"Hindsight connection: {error}", file=sys.stderr)
        sys.exit(2)
