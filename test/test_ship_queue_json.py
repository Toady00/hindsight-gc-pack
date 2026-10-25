"""Machine-readable ship queue acknowledgements preserve the native task identity."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PACK = Path(__file__).resolve().parents[1]


class QueueJSONTests(unittest.TestCase):
    def test_queue_returns_native_receipt_without_writer_or_bank_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = root / "gc"
            stub.write_text(f"#!{sys.executable}\nimport json,sys\n"
                            "a=sys.argv[1:]\n"
                            "assert a[:3]==['sling','hindsight.archivist','mol-hindsight-ship']\n"
                            "assert '--json' in a and '--formula' in a\n"
                            "print(json.dumps(dict(ok=True,success=True,molecule_id='city-ship1',dry_run=False)))\n")
            stub.chmod(0o755)
            env = {k:v for k,v in os.environ.items() if not k.startswith(("GC_","BEADS_","HINDSIGHT_"))}
            env.update(GC_BIN=str(stub))
            result = subprocess.run(["bash",str(PACK / "commands/ship/run.sh"),"--json","--bank","fixture",str(root / "docs")],
                                    cwd=root,env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)["molecule_id"],"city-ship1")
            result = subprocess.run(["bash",str(PACK / "commands/ship/run.sh"),"--json","--dry-run"],
                                    cwd=root,env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,2)
            self.assertIn("queue acknowledgements",result.stderr)


if __name__ == "__main__":
    unittest.main()
