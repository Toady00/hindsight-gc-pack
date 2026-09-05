"""Offline tests of the production scripts. No live services or user files."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

PACK = Path(__file__).resolve().parents[1]
SCRIPTS = PACK / "assets/scripts"
GIT = shutil.which("git")
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
MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, uuid
from urllib.parse import urlsplit, parse_qs, unquote
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
def value(flag): return args[args.index(flag)+1]
config = json.loads(pathlib.Path(os.environ['MOCK_CONFIG']).read_text())
stdin = sys.stdin.read() if name == 'curl' else ''
entry = dict(tool=name,args=args,api=os.environ.get('HINDSIGHT_API_URL'),key=os.environ.get('HINDSIGHT_API_KEY'),stdin=stdin)
payload_flags = {'curl':('--data-binary', '-d'), 'gc':('--metadata',)}.get(name, ())
for flag in payload_flags:
    if flag in args:
        body = value(flag)
        entry['payload'] = json.loads(pathlib.Path(body[1:]).read_text() if body.startswith('@') else body)
with open(os.environ['MOCK_LOG'], 'a') as log: log.write(json.dumps(entry)+'\n')
if name in ('curl', 'hindsight'):
    server_path = pathlib.Path(os.environ['MOCK_SERVER'])
    server = json.loads(server_path.read_text()) if server_path.exists() else {
        'documents':config.get('inventory', []), 'operations':{}}
    def save_server(): server_path.write_text(json.dumps(server))
    def operation(op_id):
        if config.get('poll_error'): sys.exit(22)
        return server['operations'].get(op_id, {'operation_id':op_id, 'status':'not_found'})
if name == 'gc':
    if args[:2] == ['rig','list']:
        assert '--json' in args
        if '--city' in args: assert value('--city') == os.environ['GC_CITY_PATH']
        if config.get('rig_error'): sys.exit(1)
        print(json.dumps(config.get('rigs', {'rigs':[]})))
    elif args[:1] == ['sling']:
        if config.get('sling_error'): sys.exit(1)
        print('queued fixture-bead')
    elif args[:1] == ['bd']:
        assert args[1:3] == ['--city', os.environ['GC_CITY_PATH']], args
        assert pathlib.Path(args[2]).is_absolute() and '--json' in args
        if config.get('beads_error'): sys.exit(1)
        db_path = pathlib.Path(os.environ['MOCK_BEADS_DB'])
        db = json.loads(db_path.read_text())
        command = args[3]
        if command == 'list':
            assert '--all' in args and value('--limit') == '0'
            result = [row for row in db['issues'] if value('--label') in row['labels']]
        elif command == 'config':
            assert args[5] == 'types.custom'
            if args[4] == 'set': db['config']['types.custom'] = args[6]
            else: assert args[4] == 'get'
            result = {'key':'types.custom', 'value':db['config']['types.custom']}
        elif command == 'create':
            assert value('--metadata').startswith('@')
            assert value('--type') in db['config']['types.custom'].split(',')
            result = dict(id='fixture-'+str(len(db['issues'])+1), title=value('--title'),
                          issue_type=value('--type'), status=value('--status'),
                          labels=value('--label').split(','), metadata=entry['payload'], assignee='')
            db['issues'].append(result)
        elif command == 'update':
            assert value('--metadata').startswith('@')
            result = next(row for row in db['issues'] if row['id'] == args[4])
            result['metadata'].update(entry['payload'])
        else: sys.exit('unexpected bd command: '+command)
        db_path.write_text(json.dumps(db))
        print(json.dumps(result))
    else: sys.exit('unexpected gc call')
elif name == 'hindsight':
    command = ' '.join(args)
    if 'operation list' in command:
        if config.get('operation_error'): sys.exit(1)
        print(json.dumps(config.get('operations', {'operations':[]})))
    elif 'operation get' in command:
        print(json.dumps(operation(args[-1])))
    elif 'document get' in command: print(json.dumps(config['document']))
    elif 'bank config' in command: print(json.dumps({'config':{'enable_auto_consolidation':config.get('auto',True)}}))
    elif 'bank consolidate' in command: print('{}')
    elif 'tag list' in command:
        tags=config.get('tags',[]); offset=int(value('--offset')); limit=int(value('--limit'))
        print(json.dumps({'items':[{'tag':t} for t in tags[offset:offset+limit]], 'total':len(tags)}))
    else: sys.exit('unexpected hindsight call: '+command)
elif name == 'curl':
    url=next(a for a in args if a.startswith('http'))
    parsed=urlsplit(url)
    method=value('--request') if '--request' in args else value('-X') if '-X' in args else 'GET'
    if parsed.path == '/openapi.json':
        properties={'operation_id':{'type':'string'}, 'async':{'type':'boolean'}}
        if config.get('capability_absent'): properties.pop('operation_id')
        print(json.dumps({'components':{'schemas':{'RetainRequest':{'properties':properties}}}}))
    elif '/operations?' in url:
        if config.get('operation_error'): sys.exit(22)
        status=parse_qs(urlsplit(url).query)['status'][0]
        ops=config.get('operations',{'operations':list(server['operations'].values())})
        if isinstance(ops,list): ops={'operations':ops}
        if not isinstance(ops,dict) or not isinstance(ops.get('operations'),list):
            print(json.dumps(ops))
        elif any(o.get('status') not in ['pending','processing','completed','failed','cancelled'] for o in ops['operations']):
            print(json.dumps(ops))
        else:
            selected=[o for o in ops['operations'] if o['status']==status]
            print(json.dumps({'operations':selected[:1], 'total':len(selected)}))
    elif method == 'POST':
        if config.get('post_error'): sys.exit(22)
        if parsed.path.endswith('/memories'):
            body=entry['payload']
            op_id=body.get('operation_id', str(uuid.uuid4()))
            uuid.UUID(op_id)
            if op_id not in server['operations']:
                server['operations'][op_id]={'operation_id':op_id, 'status':config.get('operation_status','completed')}
                # Streaming retain can stamp metadata before extraction fails.
                for item in body['items']:
                    document=dict(id=item['document_id'], document_metadata=item['metadata'],
                                  content=item['content'], tags=item['tags'])
                    server['documents']=[d for d in server['documents'] if d['id'] != document['id']]+[document]
            result={'operation_id':op_id}
        elif '/operations/' in parsed.path and parsed.path.endswith('/retry'):
            op_id=unquote(parsed.path.split('/')[-2])
            assert op_id in server['operations']
            server['operations'][op_id]['status']=config.get('retry_status','completed')
            result={'operation_id':op_id, 'success':True}
        elif '/documents/' in parsed.path and parsed.path.endswith('/reprocess'):
            document_id=unquote(parsed.path.split('/')[-2])
            assert any(d['id'] == document_id for d in server['documents'])
            op_id=str(uuid.uuid4())
            server['operations'][op_id]={'operation_id':op_id, 'status':config.get('reprocess_status','completed')}
            result={'operation_id':op_id, 'success':True}
        else: sys.exit('unexpected POST: '+url)
        save_server()
        print(json.dumps(result))
    elif '/operations/' in parsed.path:
        print(json.dumps(operation(unquote(parsed.path.split('/')[-1]))))
    elif parsed.path.endswith('/documents'):
        if config.get('inventory_error'): sys.exit(22)
        if 'inventory_response' in config: print(json.dumps(config['inventory_response']))
        else:
            query=parse_qs(parsed.query)
            offset=int(query['offset'][0]); limit=int(query['limit'][0])
            items=server['documents']
            print(json.dumps({'items':items[offset:offset+limit],'total':len(items)}))
    else: sys.exit('unexpected curl call: '+url)
    save_server()
elif name == 'git':
    if 'fetch' in args and config.get('fetch_error'): sys.exit(1)
    if 'ls-tree' in args and config.get('traversal_error'): sys.exit(1)
    if 'cat-file' in args and config.get('read_error'): sys.exit(1)
    os.execv(os.environ['REAL_GIT'], [os.environ['REAL_GIT'], *args])
elif name == 'sleep': pass
else: sys.exit('unexpected tool')
'''


class PackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hindsight-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("gc", "hindsight", "curl", "git", "sleep"):
            path = self.bin / name
            path.write_text(MOCK)
            path.chmod(0o755)
        self.config = {}
        self.server_dir = self.root / 'mock-remote-services'
        self.server_dir.mkdir()
        self.beads_db = self.server_dir / 'beads.json'
        self.beads_db.write_text(json.dumps({'config':{'types.custom':'existing-type'}, 'issues':[]}))
        self.server = self.server_dir / 'hindsight.json'
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(("HINDSIGHT_", "GC_", "GIT_", "MOCK_"))}
        self.env.update(PATH=f"{self.bin}:{os.environ['PATH']}", TMPDIR=str(self.root),
                        MOCK_CONFIG=str(self.root/"mock.json"), MOCK_LOG=str(self.root/"calls.jsonl"),
                        MOCK_BEADS_DB=str(self.beads_db), MOCK_SERVER=str(self.server),
                        REAL_GIT=GIT, HINDSIGHT_API_URL="https://fixture.invalid", HINDSIGHT_BANK="fixture",
                        HINDSIGHT_CONFIG=str(self.root/"no-config"), HINDSIGHT_STATE_DIR=str(self.root/"state"),
                        HINDSIGHT_WRITER="archivist", GC_SESSION_ID="fixture-session", GC_PACK_NAME="hindsight",
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null', GIT_ALLOW_PROTOCOL='file',
                        PYTHONDONTWRITEBYTECODE='1')
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env['GC_CITY_PATH'] = str(self.repo)
        self.remote = self.server_dir / 'repo.git'
        subprocess.run([GIT, 'init', '--bare', '-q', '-b', 'main', str(self.remote)],
                       env=self.env, check=True, capture_output=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git('remote', 'add', 'origin', str(self.remote))
        self.docs = self.repo / "docs"
        self.docs.mkdir()
        (self.docs/"spec.md").write_text(DOC)
        self.commit()

    def git(self, *args):
        return subprocess.run([GIT, "-C", str(self.repo), *args], env=self.env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
        self.git('-c', 'core.hooksPath=/dev/null', 'push', '-q', 'origin', 'main')

    def run_script(self, path, *args, input=None, code=0):
        (self.root/"mock.json").write_text(json.dumps(self.config))
        command = [str(path), *map(str,args)]
        if str(path).endswith(".py"):
            command.insert(0, "python3")
        result = subprocess.run(command, input=input, env=self.env, cwd=self.repo, text=True, capture_output=True, timeout=20)
        if code is not None:
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def ship(self, *args, code=0):
        return self.run_script(SCRIPTS/"ship-docs.sh", *args, self.docs, code=code)

    def calls(self, name=None):
        path=self.root/"calls.jsonl"
        rows=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if name is None or row['tool']==name]

    def posts(self):
        return [r for r in self.calls("curl") if any(
            flag in r['args'] and r['args'][r['args'].index(flag)+1] == 'POST'
            for flag in ('-X', '--request'))]

    def payload(self):
        return next(r['payload']['items'][0] for r in reversed(self.posts())
                    if 'items' in r.get('payload', {}))

    def bead(self, kind='bank', document_id=''):
        rows = [row for row in json.loads(self.beads_db.read_text())['issues']
                if row['metadata']['hindsight']['kind'] == kind
                and row['metadata']['hindsight']['document_id'] == document_id]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def report(self, key='latest_run'):
        return self.bead('bank')['metadata']['hindsight']['data'].get(key)

    def document_state(self, document_id='spec.fixture'):
        return self.bead('document', document_id)['metadata']['hindsight']['data']

    def derive(self, doc):
        return json.loads(self.run_script(PACK/"schemas/docs/derive", input=doc).stdout)

    def test_schema_requires_status_on_every_type(self):
        types=json.loads((PACK/"schemas/docs/audit-vocab.json").read_text())["closed"]["memory_type"]
        for kind in types:
            with self.subTest(kind=kind):
                text=DOC.replace("type: spec",f"type: {kind}")
                self.assertEqual(self.derive(text)["verdict"],"ship")
                self.assertEqual(self.derive(text.replace("status: draft\n",""))["verdict"],"refuse")

    def test_null_schema_also_requires_one_status(self):
        doc='---\nhindsight:\n  id: raw.fixture\n  strategy: design-record\n  tags: [team:eng]\n---\nBody'
        result=self.run_script(PACK/'schemas/null/derive',input=doc)
        self.assertEqual(json.loads(result.stdout)['verdict'],'refuse')
        result=self.run_script(PACK/'schemas/null/derive',input=doc.replace('team:eng','team:eng, status:draft'))
        self.assertEqual(json.loads(result.stdout)['verdict'],'ship')

    def test_schema_type_controls_strategy_status_controls_context(self):
        for status in ("draft","accepted","superseded","deprecated"):
            v=self.derive(DOC.replace("status: draft",f"status: {status}"))
            self.assertEqual(v["strategy"],"design-record")
            self.assertIn(f"status:{status}",v["tags"])
            self.assertIn(status.upper(),v["context"])
            self.assertFalse(any("status:" in tag for scope in v["observation_scopes"] for tag in scope))

    def test_schema_rejects_bad_fields(self):
        for old,new in [("id: spec.fixture\n",""),("updated_at: 2026-09-04T00:00:00Z","updated_at: nonsense"),
                        ("2026-09-04T00:00:00Z","2026-02-30T00:00:00Z"),("repos: [repo]","repos: repo"),
                        ("repos: [repo]","repos: [null]"),("repos: [repo]","repos: []"),
                        ("source: agent","source: true"),("title: Fixture","title: ''"),
                        ("schema_version: 2","schema_version: 3")]:
            with self.subTest(new=new): self.assertEqual(self.derive(DOC.replace(old,new))["verdict"],"refuse")
        self.assertEqual(self.derive("# ordinary markdown")["verdict"],"skip")
        self.assertEqual(self.derive("---\nlayout: page\n---\nhello")["verdict"],"skip")
        self.assertEqual(self.derive("---\nid: incomplete")["verdict"],"refuse")

    def test_ship_content_hash_status_and_poll(self):
        self.ship()
        payload=self.payload()
        self.assertEqual(payload["content"],DOC.split("---\n")[-1])
        self.assertEqual(payload["metadata"]["content_hash"],hashlib.sha256(DOC.encode()).hexdigest())
        self.assertIn("status:draft",payload["tags"])
        operation_id=self.posts()[0]['payload']['operation_id']
        uuid.UUID(operation_id)
        self.assertIs(self.posts()[0]['payload']['async'],True)
        self.assertTrue(any(a.endswith('/operations/'+operation_id) for c in self.calls('curl') for a in c['args']))
        self.assertEqual(self.document_state()['last_success']['operation_id'],operation_id)
        self.assertFalse(self.calls('hindsight'))
        for call in self.posts():
            args=call['args']
            self.assertFalse(Path(args[args.index('--data-binary')+1][1:]).exists())

    def test_unchanged_skip_and_explicit_reprocess(self):
        self.config['inventory']=[dict(id='spec.fixture', document_metadata=dict(content_hash=hashlib.sha256(DOC.encode()).hexdigest(),repo='repo',relpath='docs/spec.md'))]
        self.ship()
        self.assertEqual(len(self.posts()),1, 'A stamped inventory hash alone is not a receipt')
        receipt=self.document_state()['last_success']
        self.ship()
        self.assertEqual(len(self.posts()),1)
        self.assertEqual(self.report()['counts']['skipped'],1)
        self.assertEqual(self.document_state()['last_success'],receipt)
        self.ship("--reprocess")
        self.assertEqual(len(self.posts()),3)
        self.assertEqual(self.posts()[-2]['payload']['items'][0],self.posts()[0]['payload']['items'][0])
        self.assertNotEqual(self.posts()[-2]['payload']['operation_id'],receipt['operation_id'])
        self.assertTrue(any(a.endswith('/documents/spec.fixture/reprocess') for a in self.posts()[-1]['args']))
        self.assertNotIn('payload',self.posts()[-1])
        self.assertIn('reprocess_operation_id',self.document_state()['last_success'])

    def test_changed_status_reships_without_timestamp_bump(self):
        self.ship()
        (self.docs/'spec.md').write_text(DOC.replace('status: draft','status: accepted'))
        self.commit(); self.ship()
        self.assertEqual(len(self.posts()),2)
        self.assertIn('status:accepted',self.payload()['tags'])

    def test_dry_run_does_not_write_or_persist_report(self):
        self.env.pop('HINDSIGHT_WRITER'); self.env.pop('GC_SESSION_ID')
        before=self.beads_db.read_text()
        self.ship('--dry-run')
        self.assertFalse(self.posts()); self.assertFalse((self.root/'state').exists())
        self.assertEqual(self.beads_db.read_text(),before)
        self.assertFalse(any(c['args'][3] in ('create','update','config') for c in self.calls('gc')))

    def test_raw_writers_refuse_other_sessions(self):
        self.env['HINDSIGHT_WRITER']='worker'
        self.ship(code=2)
        self.run_script(SCRIPTS/'memory-retain.sh','--id','gotcha.fixture','--title','Fixture','--repos','repo',input='content',code=2)
        self.run_script(SCRIPTS/'bank-maintain.sh',code=2)
        self.assertFalse(self.posts())

    def test_manual_ship_queues_without_api_calls(self):
        self.env.pop('HINDSIGHT_WRITER'); self.env.pop('GC_SESSION_ID'); self.env.pop('HINDSIGHT_BANK')
        path='docs with spaces/$(do-not-execute)'
        self.run_script(PACK/'commands/ship/run.sh','--ref','branch with spaces',path)
        calls=self.calls(); self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['args'][:3],['sling','hindsight.archivist','mol-hindsight-ship'])
        encoded=calls[0]['args'][-1].split('=',1)[1]
        args=json.loads(base64.b64decode(encoded))
        self.assertIn(str(self.repo/path),args)
        self.assertIn('branch with spaces',args)

    def test_queue_failure_is_not_success(self):
        self.env.pop('HINDSIGHT_WRITER'); self.config['sling_error']=True
        r=self.run_script(PACK/'commands/ship/run.sh',code=1)
        self.assertNotIn('Queued ship request',r.stdout)

    def test_queue_never_records_credentials_in_url(self):
        self.env.pop('HINDSIGHT_WRITER')
        result=self.run_script(PACK/'commands/ship/run.sh','--api','https://user:fixture-secret@fixture.invalid',code=2)
        self.assertFalse(self.calls())
        self.assertNotIn('fixture-secret',result.stderr)

    def test_archivist_executes_request_without_recursive_queue(self):
        request=base64.b64encode(json.dumps(['--bank','fixture',str(self.docs)]).encode()).decode()
        self.run_script(PACK/'commands/ship/run.sh','--request',request)
        self.assertEqual(len(self.posts()),1)
        self.assertFalse(any(c['args'][0]=='sling' for c in self.calls('gc')))

    def test_request_rejects_unrecognized_flags(self):
        request=base64.b64encode(json.dumps(['--full-scan']).encode()).decode()
        self.run_script(PACK/'commands/ship/run.sh','--request',request,code=2)
        self.assertFalse(self.posts())

    def test_read_adapter_uses_alias_and_rejects_writes(self):
        self.env['HINDSIGHT_API']='https://read.invalid'
        self.run_script(SCRIPTS/'hindsight-read.sh','-o','json','operation','list','fixture')
        self.assertEqual(self.calls('hindsight')[-1]['api'],'https://read.invalid')
        self.run_script(SCRIPTS/'hindsight-read.sh','memory','retain','fixture','body',code=2)
        self.assertEqual(len(self.calls('hindsight')),1)

    def test_connection_same_endpoint_and_auth_for_all_clients(self):
        self.env['HINDSIGHT_API']='https://other.invalid'
        self.env['HINDSIGHT_API_KEY']='fixture-secret'
        self.ship('--api','https://override.invalid/')
        for call in self.calls('hindsight')+self.calls('curl'):
            self.assertEqual(call['api'],'https://override.invalid')
            self.assertEqual(call['key'],'fixture-secret')
        for call in self.calls('curl'):
            self.assertIn('Authorization: Bearer fixture-secret',call['stdin'])
            self.assertNotIn('fixture-secret',' '.join(call['args']))
            self.assertTrue(any(a.startswith('https://override.invalid/') for a in call['args']))

    def test_connection_config_and_no_default_server(self):
        self.env.pop('HINDSIGHT_API_URL')
        with self.subTest(connection='missing'):
            self.ship(code=2)
            self.assertFalse(self.calls())
        config=self.root/'connection.toml'; config.write_text('api_url = "https://configured.invalid/"\napi_key = "fixture-token"\n')
        self.env['HINDSIGHT_CONFIG']=str(config)
        self.ship()
        self.assertEqual(self.calls('curl')[-1]['api'],'https://configured.invalid')
        self.assertEqual(self.calls('curl')[-1]['key'],'fixture-token')

    def test_failed_or_malformed_drain_stops_ship(self):
        for config in ({'operation_error':True},{'operations':{}},{'operations':{'operations':[{'status':'mystery'}]}}):
            with self.subTest(config=config):
                self.config=config; self.ship(code=5)
                self.assertFalse(self.posts())
                self.assertEqual(self.report()['status'],'incomplete')

    def test_pending_operations_stop_both_writers(self):
        self.config['operations']={'operations':[{'status':'processing'}]}
        self.ship('--drain-timeout','0',code=3)
        self.run_script(SCRIPTS/'memory-retain.sh','--id','gotcha.fixture','--title','Fixture','--repos','repo','--drain-timeout','0',input='body',code=3)
        self.assertFalse(self.posts())

    def test_drain_finds_old_pending_operation_beyond_recent_page(self):
        self.config['operations']={'operations':[{'status':'completed'}]*50+[{'status':'pending'}]}
        self.ship('--drain-timeout','0',code=3)
        self.assertFalse(self.posts())

    def test_inventory_error_does_not_ship(self):
        self.config['inventory_response']={'error':'unauthorized'}
        self.ship(code=5); self.assertFalse(self.posts())

    def test_malformed_document_metadata_never_records_scan_success(self):
        self.config['inventory'] = [dict(id='spec.fixture', document_metadata='invalid')]
        self.ship('--full-scan', code=5)
        self.assertFalse(self.posts())
        self.assertEqual(self.report()['status'], 'incomplete')
        self.assertIsNone(self.report('last_success'))

    def test_reprocess_after_recovering_ordinary_attempt_is_not_lost(self):
        self.config['operation_status'] = 'failed'
        self.ship(code=1)
        self.config['operation_status'] = 'completed'
        self.ship('--reprocess')
        self.assertTrue(any(arg.endswith('/documents/spec.fixture/reprocess')
                            for call in self.posts() for arg in call['args']))

    def test_corrupt_shared_health_fails_closed(self):
        self.ship('--full-scan')
        db = json.loads(self.beads_db.read_text())
        bank = next(row for row in db['issues'] if row['issue_type'] == 'hindsight-bank')
        bank['metadata']['hindsight']['data']['last_success']['finished_at'] = None
        self.beads_db.write_text(json.dumps(db))
        result = self.run_script(PACK/'commands/status/run.sh', code=5)
        self.assertNotIn('Traceback', result.stderr)

    def test_inventory_paginates(self):
        self.config['inventory']=[{'id':f'other.{i}'} for i in range(501)]
        self.ship('--dry-run')
        pages=[a for c in self.calls('curl') for a in c['args'] if '/documents?' in a]
        self.assertEqual([url.split('offset=')[1] for url in pages],['0','100','200','300','400','500'])

    def test_failed_extraction_and_poll_failure_are_reported(self):
        self.config['operation_status']='failed'; self.ship(code=1)
        self.assertEqual(self.report()['counts']['failed'],1)
        self.assertEqual(self.report()['counts']['shipped'],0)
        self.config={'poll_error':True}; self.ship(code=5)
        self.assertEqual(self.report()['status'],'incomplete')
        self.assertNotIn('last_success',self.document_state())

    def test_http_failure_does_not_record_success(self):
        self.config['post_error']=True
        self.ship(code=5)
        self.assertEqual(self.report()['documents'][0]['status'],'failed')
        self.assertNotIn('last_success',self.document_state())

    def test_invalid_doc_is_reported_with_id(self):
        self.config['inventory']=[dict(id='spec.fixture',document_metadata=dict(repo='repo',relpath='docs/spec.md'))]
        (self.docs/'spec.md').write_text(DOC.replace('status: draft\n',''))
        self.commit(); self.ship(code=1)
        self.assertFalse(self.posts())
        self.assertEqual(self.report()['documents'][0]['id'],'spec.fixture')
        self.assertEqual(self.report()['documents'][0]['status'],'refused')
        self.assertEqual(self.report()['counts']['gone'],0)

    def test_partial_root_does_not_report_unwalked_docs_gone(self):
        self.config['inventory']=[dict(id='other',document_metadata=dict(repo='repo',relpath='other-docs/file.md'))]
        self.ship()
        self.assertEqual(self.report()['counts']['gone'],0)

    def test_gone_does_not_advance_full_success(self):
        self.config['inventory']=[dict(id='gone',document_metadata=dict(repo='repo',relpath='docs/removed.md'))]
        self.ship('--full-scan')
        self.assertEqual(self.report()['counts']['gone'],1)
        self.assertEqual(self.report()['status'],'incomplete')
        self.assertIsNone(self.report('last_success'))

    def test_missing_root_reports_incomplete(self):
        self.ship(self.root/'missing',code=5)
        self.assertTrue(any(r['status']=='failed' for r in self.report()['roots']))

    def test_failed_fetch_never_ships_stale_checkout(self):
        self.config['fetch_error']=True
        self.ship(code=5)
        self.assertEqual(self.report()['roots'][0]['status'],'failed')
        self.assertFalse(self.posts())
        self.assertFalse(self.calls('curl'))

    def test_full_scan_health_and_partial_scan_preserves_success(self):
        self.env['GC_CITY_PATH']=str(self.repo)
        self.run_script(PACK/'commands/ship/run.sh')
        success=self.report('last_success')
        self.ship()
        self.assertEqual(self.report('last_success'),success)
        self.run_script(SCRIPTS/'ship-report.py','status',self.env['HINDSIGHT_API_URL'],'fixture')
        self.env['HINDSIGHT_MAX_SHIP_AGE']='0'
        self.run_script(SCRIPTS/'ship-report.py','status',self.env['HINDSIGHT_API_URL'],'fixture',code=1)

    def test_failed_discovery_persists_incomplete_full_scan(self):
        self.config['rig_error']=True
        self.run_script(PACK/'commands/ship/run.sh',code=5)
        self.assertEqual(self.report('latest_full_scan')['status'],'incomplete')
        self.assertIn('discover',self.report()['roots'][0]['detail'])
        self.assertFalse(self.posts())

    def test_missing_registered_rig_is_not_silently_omitted(self):
        self.env['GC_CITY_PATH']=str(self.repo)
        self.config['rigs']={'rigs':[{'name':'gone-rig','path':str(self.root/'missing-rig')}]}
        self.run_script(PACK/'commands/ship/run.sh',code=5)
        self.assertEqual(self.report()['counts']['incomplete'],1)
        self.assertEqual(self.report('latest_full_scan')['status'],'incomplete')

    def test_registered_rig_cannot_silently_use_the_city_repository(self):
        rig = self.repo / 'not-a-repository'
        rig.mkdir()
        self.config['rigs'] = {'rigs': [{'name': 'rig', 'path': str(rig)}]}
        result = self.run_script(PACK/'commands/ship/run.sh', code=5)
        self.assertFalse(self.posts())
        self.assertIn('own Git checkout', result.stderr)
        self.assertEqual(self.report()['status'], 'incomplete')

    def test_status_command_reads_reports_without_bank_calls(self):
        self.ship('--full-scan')
        count=len(self.calls('curl'))+len(self.calls('hindsight'))
        before=self.beads_db.read_text()
        self.run_script(PACK/'commands/status/run.sh')
        self.assertEqual(len(self.calls('curl'))+len(self.calls('hindsight')),count)
        self.assertEqual(self.beads_db.read_text(),before)

    def test_abandoned_run_is_unhealthy(self):
        self.ship('--full-scan')
        db=json.loads(self.beads_db.read_text())
        bank=next(row for row in db['issues'] if row['issue_type']=='hindsight-bank')
        data=bank['metadata']['hindsight']['data']
        # Simulate the server state left by a writer killed after ScanReport.start.
        data['latest_run']=dict(run_id=str(uuid.uuid4()),status='running',full_scan=True)
        data['latest_full_scan']=dict(data['latest_run'])
        self.beads_db.write_text(json.dumps(db))
        self.run_script(PACK/'commands/status/run.sh',code=1)

    def test_failed_retain_stamps_hash_but_recovers_original_operation(self):
        self.config['operation_status']='failed'
        self.ship('--full-scan',code=1)
        attempt=self.document_state()['attempt']
        operation_id=attempt['operation_id']
        source_hash=hashlib.sha256(DOC.encode()).hexdigest()
        server=json.loads(self.server.read_text())
        self.assertEqual(server['documents'][0]['document_metadata']['content_hash'],source_hash)
        self.assertEqual(server['operations'][operation_id]['status'],'failed')
        self.assertEqual(attempt['state'],'failed')
        self.assertNotIn('last_success',self.document_state())
        self.assertIsNone(self.report('last_success'))

        self.config.clear()
        self.ship('--full-scan')
        posts=self.posts()
        self.assertEqual(len(posts),2)
        self.assertTrue(any(a.endswith('/operations/'+operation_id+'/retry') for a in posts[-1]['args']))
        self.assertNotIn('payload',posts[-1])
        state=self.document_state()
        self.assertEqual(state['attempt']['operation_id'],operation_id)
        self.assertEqual(state['attempt']['state'],'succeeded')
        self.assertEqual(state['last_success']['operation_id'],operation_id)
        self.assertEqual(state['last_success']['source_hash'],source_hash)
        self.assertEqual(self.report()['counts']['recovered'],1)
        self.assertEqual(self.report('last_success')['status'],'ok')
        self.assertEqual(json.loads(self.server.read_text())['operations'][operation_id]['status'],'completed')

    def test_unknown_operation_replays_saved_intent(self):
        self.config['post_error']=True
        self.ship(code=5)
        operation_id=self.document_state()['attempt']['operation_id']
        self.assertNotIn(operation_id,json.loads(self.server.read_text())['operations'])
        self.config.clear()
        self.ship()
        self.assertEqual(len(self.posts()),2)
        self.assertEqual(self.posts()[0]['payload'],self.posts()[1]['payload'])
        self.assertEqual(self.document_state()['last_success']['operation_id'],operation_id)

    def test_second_machine_skips_using_shared_receipt_without_local_state(self):
        self.ship('--full-scan')
        receipt=self.document_state()['last_success']
        success=self.report('last_success')
        first_city=self.env['GC_CITY_PATH']
        first_state=Path(self.env['HINDSIGHT_STATE_DIR'])
        self.assertFalse(first_state.exists())
        machine=self.root/'second-machine'
        machine.mkdir()
        scratch=machine/'tmp'
        scratch.mkdir()
        checkout=machine/'checkout'
        subprocess.run([GIT,'clone','-q',str(self.remote),str(checkout)],env=self.env,
                       check=True,capture_output=True)
        self.repo=checkout
        self.docs=checkout/'docs'
        self.env.update(GC_CITY_PATH=str(checkout),GC_SESSION_ID='second-session',TMPDIR=str(scratch),
                        HINDSIGHT_STATE_DIR=str(machine/'state'))
        before=len(self.calls())
        self.ship()
        self.assertEqual(len(self.posts()),1)
        self.assertEqual(self.report()['counts']['skipped'],1)
        self.assertEqual(self.document_state()['last_success'],receipt)
        self.assertEqual(self.report('last_success'),success)
        self.assertNotEqual(self.env['GC_CITY_PATH'],first_city)
        for call in self.calls()[before:]:
            if call['tool']=='gc' and call['args'][0]=='bd':
                self.assertEqual(call['args'][1:3],['--city',str(checkout)])
        self.assertFalse(first_state.exists())
        self.assertFalse((machine/'state').exists())
        self.assertEqual(list(scratch.iterdir()),[])
        self.run_script(PACK/'commands/status/run.sh')

    def test_beads_outage_prevents_any_retain(self):
        self.config['beads_error']=True
        result=self.ship(code=5)
        self.assertIn('Beads',result.stderr)
        self.assertFalse(self.calls('curl'))
        self.assertFalse(self.calls('hindsight'))
        self.assertEqual(json.loads(self.beads_db.read_text())['issues'],[])

    def test_altered_pinned_document_record_is_rejected(self):
        self.ship()
        original=self.beads_db.read_text()
        for alteration in ({'status':'open'}, {'issue_type':'task'}, {'assignee':'worker'},
                           {'ephemeral':True}, {'labels':['gc:agent']},
                           {'metadata':{'gc.routed_to':'worker'}}):
            with self.subTest(alteration=alteration):
                db=json.loads(original)
                row=next(r for r in db['issues'] if r['issue_type']=='hindsight-document')
                for key,value in alteration.items():
                    if key=='metadata': row[key].update(value)
                    elif key=='labels': row[key].extend(value)
                    else: row[key]=value
                self.beads_db.write_text(json.dumps(db))
                result=self.ship(code=5)
                self.assertIn('persistent, pinned, unassigned and unrouted',result.stderr)
                self.assertEqual(len(self.posts()),1)
                self.assertEqual(self.report()['status'],'incomplete')

    def test_missing_operation_id_capability_refuses_write(self):
        self.config['capability_absent']=True
        result=self.ship(code=2)
        self.assertIn('does not advertise client operation_id',result.stderr)
        self.assertFalse(self.posts())
        self.assertEqual(self.report()['counts']['failed'],1)
        self.assertIsNone(self.report('last_success'))
        self.assertTrue(any(a=='https://fixture.invalid/openapi.json' for c in self.calls('curl') for a in c['args']))

    def test_duplicate_document_ids_refuse_both_before_first_write(self):
        (self.docs/'duplicate.md').write_text(DOC.replace('Fixture','Different title'))
        self.commit()
        self.ship('--full-scan',code=1)
        self.assertFalse(self.posts())
        self.assertEqual(self.report()['counts']['shipped'],0)
        self.assertEqual(self.report()['counts']['refused'],1)
        self.assertIn('multiple published files',self.report()['documents'][0]['detail'])
        self.assertIsNone(self.report('last_success'))

    def test_failed_traversal_never_reports_empty_success_or_gone(self):
        self.config.update(traversal_error=True,inventory=[
            dict(id='gone',document_metadata=dict(repo='repo',relpath='docs/removed.md'))])
        result=self.ship('--full-scan',code=5)
        self.assertIn('git ls-tree failed',result.stdout)
        self.assertEqual(self.report()['counts']['incomplete'],1)
        self.assertEqual(self.report()['counts']['gone'],0)
        self.assertEqual(self.report()['status'],'incomplete')
        self.assertIsNone(self.report('last_success'))
        self.assertFalse(self.posts())
        self.assertFalse(self.calls('curl'))

    def test_blob_read_failure_does_not_report_gone_or_advance_success(self):
        self.ship('--full-scan')
        success=self.report('last_success')
        self.config['read_error']=True
        result=self.ship('--full-scan',code=5)
        self.assertIn('git cat-file failed',result.stdout)
        self.assertEqual(self.report()['counts']['gone'],0)
        self.assertEqual(self.report()['counts']['shipped'],0)
        self.assertEqual(self.report()['counts']['incomplete'],1)
        self.assertEqual(self.report()['status'],'incomplete')
        self.assertEqual(self.report('last_success'),success)
        self.assertEqual(len(self.posts()),1)

    def test_local_dirty_branch_and_unpushed_commit_are_excluded(self):
        published=self.git('rev-parse','HEAD')
        self.git('checkout','-qb','private')
        (self.docs/'spec.md').write_text(DOC.replace('90 seconds','PRIVATE COMMIT'))
        self.git('add','.')
        self.git('-c','commit.gpgsign=false','-c','core.hooksPath=/dev/null','commit','-qm','unpushed')
        (self.docs/'spec.md').write_text(DOC.replace('90 seconds','DIRTY WORKTREE'))
        (self.docs/'untracked.md').write_text(DOC.replace('spec.fixture','spec.untracked'))
        self.ship()
        self.assertEqual(self.payload()['content'],DOC.split('---\n')[-1])
        self.assertEqual(self.payload()['metadata']['source_commit'],published)
        self.assertEqual(self.report()['roots'][0]['ref'],'refs/heads/main')
        self.assertEqual(len(self.posts()),1)
        self.ship()
        self.assertEqual(len([c for c in self.calls('git') if 'fetch' in c['args']]),2)
        self.assertEqual(self.git('branch','--show-current'),'private')
        self.assertIn('DIRTY WORKTREE',(self.docs/'spec.md').read_text())
        self.assertTrue((self.docs/'untracked.md').exists())

    def test_explicit_ref_fetches_published_branch(self):
        self.git('checkout','-qb','published')
        (self.docs/'spec.md').write_text(DOC.replace('status: draft','status: accepted'))
        self.git('add','.')
        self.git('-c','commit.gpgsign=false','-c','core.hooksPath=/dev/null','commit','-qm','published')
        self.git('push','-q','origin','published')
        published=self.git('rev-parse','HEAD')
        self.git('checkout','-q','main')
        self.ship('--ref','origin/published')
        self.assertIn('status:accepted',self.payload()['tags'])
        self.assertEqual(self.payload()['metadata']['source_commit'],published)
        self.assertEqual(self.report()['roots'][0]['ref'],'refs/heads/published')

    def test_fetch_reads_remote_changes_without_updating_local_branch(self):
        checkout=self.repo
        local_head=self.git('rev-parse','HEAD')
        publisher=self.root/'publisher'
        subprocess.run([GIT,'clone','-q',str(self.remote),str(publisher)],env=self.env,
                       check=True,capture_output=True)
        try:
            self.repo=publisher
            self.git('config','user.name','Fixture')
            self.git('config','user.email','fixture@example.invalid')
            (publisher/'docs/spec.md').write_text(DOC.replace('status: draft','status: accepted'))
            self.commit()
            published=self.git('rev-parse','HEAD')
        finally:
            self.repo=checkout
        self.assertEqual(self.git('rev-parse','origin/main'),local_head)
        self.ship()
        self.assertEqual(self.payload()['metadata']['source_commit'],published)
        self.assertIn('status:accepted',self.payload()['tags'])
        self.assertEqual(self.git('rev-parse','HEAD'),local_head)
        self.assertEqual(self.git('rev-parse','origin/main'),local_head)

    def test_registered_rig_docs_absent_locally_still_ship_published_docs(self):
        shutil.rmtree(self.docs)
        self.config['rigs']={'rigs':[{'name':'repo','path':str(self.repo)}]}
        self.run_script(PACK/'commands/ship/run.sh')
        self.assertEqual(len(self.posts()),1)
        self.assertEqual(self.payload()['document_id'],'spec.fixture')
        self.assertEqual(self.report('last_success')['status'],'ok')
        self.assertFalse(self.docs.exists())
        registry=[c for c in self.calls('gc') if c['args'][:2]==['rig','list']]
        self.assertEqual(registry[0]['args'],['rig','list','--json','--city',str(self.repo)])

    def test_empty_published_docs_root_is_complete_without_local_directory(self):
        self.git('rm','-qr','docs')
        self.commit()
        self.config['rigs']={'rigs':[{'name':'repo','path':str(self.repo)}]}
        self.run_script(PACK/'commands/ship/run.sh')
        self.assertFalse(self.posts())
        self.assertFalse(self.docs.exists())
        self.assertEqual(self.report()['roots'][0]['status'],'ok')
        self.assertEqual(self.report('last_success')['status'],'ok')

    def test_city_docs_absent_locally_still_ship_published_docs(self):
        shutil.rmtree(self.docs)
        self.run_script(SCRIPTS/'ship_docs.py')
        self.assertEqual(len(self.posts()),1)
        self.assertEqual(self.payload()['document_id'],'spec.fixture')
        self.assertEqual(self.report('last_success')['status'],'ok')
        self.assertFalse(self.docs.exists())

    def test_shared_beads_preserve_custom_types_and_unrelated_metadata(self):
        self.ship()
        db=json.loads(self.beads_db.read_text())
        self.assertEqual(set(db['config']['types.custom'].split(',')),
                         {'existing-type','hindsight-bank','hindsight-document'})
        for row in db['issues']:
            self.assertEqual(row['status'],'pinned')
            self.assertIn('hindsight:records',row['labels'])
            self.assertFalse(row['assignee'])
            self.assertNotIn('gc.routed_to',row['metadata'])
            row['metadata']['unrelated']='keep me'
        self.beads_db.write_text(json.dumps(db))
        self.ship('--reprocess')
        for row in json.loads(self.beads_db.read_text())['issues']:
            self.assertEqual(row['metadata']['unrelated'],'keep me')
        self.assertFalse((self.root/'state').exists())

    def test_maintenance_failure_never_consolidates(self):
        self.config['operation_error']=True
        self.run_script(SCRIPTS/'bank-maintain.sh',code=5)
        self.assertFalse(any('consolidate' in c['args'] for c in self.calls('hindsight')))

    def test_audit_pages_and_uses_rig_registry(self):
        # Short unique values avoid quadratic near-duplicate noise.
        tags=[f'domain:{i:03x}' for i in range(500)]+['repo:unknown']
        self.config.update(tags=tags,rigs={'rigs':[{'name':'repo','path':str(self.repo)}]})
        r=self.run_script(SCRIPTS/'bank-maintain.sh','--skip-consolidate',code=2)
        self.assertIn('UNKNOWN-RIG   repo:unknown',r.stdout)
        self.assertEqual(len([c for c in self.calls('hindsight') if 'tag' in c['args']]),2)

    def test_audit_reports_false_auto_consolidation(self):
        self.config['auto']=False
        r=self.run_script(SCRIPTS/'bank-maintain.sh','--skip-consolidate',code=2)
        self.assertIn('enable_auto_consolidation=false',r.stdout)

    def test_clean_empty_audit(self):
        self.run_script(SCRIPTS/'bank-maintain.sh','--skip-consolidate')

    def test_registry_unavailable_is_audit_finding(self):
        self.config['rig_error']=True
        r=self.run_script(SCRIPTS/'bank-maintain.sh','--skip-consolidate',code=2)
        self.assertIn('AUDIT-INCOMPLETE',r.stdout)

    def test_memory_has_status_and_uses_shared_strategy(self):
        self.run_script(SCRIPTS/'memory-retain.sh','--id','gotcha.fixture','--title','Fixture','--repos','repo',input='Symptom, cause, fix.')
        p=self.payload()
        self.assertIn('status:accepted',p['tags']); self.assertEqual(p['strategy'],'gotcha')
        self.assertNotIn('repo',p['metadata'])

    def test_memory_bump_preserves_status_and_reads_after_drain(self):
        self.config['document']=dict(content='Useful body',document_metadata=dict(hit_count='2',title='Fixture'),tags=['scope:repo','repo:repo','source:agent','memory_type:gotcha','status:deprecated'])
        self.run_script(SCRIPTS/'memory-retain.sh','--id','gotcha.fixture','--bump')
        self.assertIn('status:deprecated',self.payload()['tags'])
        self.assertEqual(self.payload()['metadata']['hit_count'],'3')
        calls=self.calls()
        self.assertEqual(calls[0]['tool'],'curl')
        self.assertTrue(any('/operations?' in a for a in calls[0]['args']))

    def test_legacy_memory_bump_needs_explicit_status(self):
        self.config['document']=dict(content='Body',document_metadata=dict(title='Fixture'),tags=['scope:repo','repo:repo','source:agent','memory_type:gotcha'])
        self.run_script(SCRIPTS/'memory-retain.sh','--id','gotcha.fixture','--bump',code=2)
        self.assertFalse(self.posts())


if __name__ == '__main__':
    unittest.main()
