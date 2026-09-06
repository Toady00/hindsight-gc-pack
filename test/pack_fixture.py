"""Small external-service fixture for the few tests that cross the real shell."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PACK = Path(__file__).resolve().parents[1]
SCRIPTS = PACK / "assets/scripts"
DOC = """---
schema_version: 2
id: spec.fixture
type: spec
title: Fixture
status: draft
source: agent
scope: repo
repos: [repo]
updated_at: 2026-09-04T00:00:00Z
---
Exact limit: 90 seconds. Keep this body intact.
"""


class ShellFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hindsight-shell-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("gc", "curl", "hindsight"):
            path = self.bin / name
            path.write_text(f'#!{sys.executable}\n' + (PACK / "test/assets/pack_service.py").read_text())
            path.chmod(0o755)
        self.config = {}
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("HINDSIGHT_", "GC_", "GIT_", "MOCK_", "BEADS_", "BD_"))}
        self.env.update(PATH=f"{self.bin}:{Path(sys.executable).parent}:{os.environ['PATH']}",
                        HOME=str(self.root), TMPDIR=str(self.root), MOCK_ROOT=str(self.root),
                        HINDSIGHT_API_URL="https://fixture.invalid", HINDSIGHT_BANK="fixture",
                        HINDSIGHT_CONFIG=str(self.root / "no-config"),
                        HINDSIGHT_WRITER="archivist", GC_SESSION_ID="fixture-session",
                        GC_CITY_PATH=str(self.root), GC_PACK_NAME="hindsight",
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
                        GIT_ALLOW_PROTOCOL="file", PYTHONDONTWRITEBYTECODE="1")
        (self.root / "beads.json").write_text(json.dumps({"types": "existing-type", "issues": []}))
        (self.root / "server.json").write_text(json.dumps({"documents": [], "operations": {}}))

    def run_script(self, path, *args, input=None, code=0):
        (self.root / "config.json").write_text(json.dumps(self.config))
        result = subprocess.run([str(path), *map(str, args)], input=input, env=self.env,
                                cwd=self.root, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def calls(self, tool=None):
        path = self.root / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [c for c in calls if tool is None or c["tool"] == tool]

    def posts(self):
        return [c for c in self.calls("curl") if "POST" in c["args"]]

    def state(self, document_id=""):
        kind = "document" if document_id else "bank"
        rows = [r["metadata"]["hindsight"] for r in json.loads((self.root / "beads.json").read_text())["issues"]]
        matches = [r["data"] for r in rows if r["kind"] == kind and r["document_id"] == document_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def published_docs(self):
        git = shutil.which("git")
        repo, remote = self.root / "repo", self.root / "origin.git"
        subprocess.run([git, "init", "--bare", "-q", "-b", "main", str(remote)],
                       env=self.env, check=True, capture_output=True)
        subprocess.run([git, "clone", "-q", str(remote), str(repo)],
                       env=self.env, check=True, capture_output=True)
        docs = repo / "docs"
        docs.mkdir()
        (docs / "spec.md").write_text(DOC)
        for args in (("add", "."),
                     ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                      "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"),
                     ("-c", "core.hooksPath=/dev/null", "push", "-q", "origin", "main")):
            subprocess.run([git, "-C", str(repo), *args], env=self.env, check=True, capture_output=True)
        self.env["GC_CITY_PATH"] = str(repo)
        return docs
