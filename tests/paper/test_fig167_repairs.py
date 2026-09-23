import collections
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'ae'))
from runners.cube_memory import cpuset, require_tmpfs
from replay.search_order import reorder, build_table, restore_search_order
from replay.legacy_schedule import recorded_diff_op
spec=importlib.util.spec_from_file_location('audit_fig167',ROOT/'ae/scripts/audit_fig167.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)

class Figure167Repairs(unittest.TestCase):
    def test_semantic_fallback_recording_does_not_abort_empty_literal_search(self):
        # Django-12915 searches tests, then semantic fallback returns handlers.py.
        # Those fallback hits must not be required in the preceding literal scan.
        query = ('ASGIStaticFilesHandler', '**/test*.py')
        with patch('replay.search_order.table', return_value={query:['django/contrib/staticfiles/handlers.py']}):
            self.assertEqual(restore_search_order(iter([]), *query), [])
            hits = [('tests/test_static.py', 18), ('tests/test_static.py', 18)]
            self.assertEqual(restore_search_order(iter(hits), *query), hits)

    def test_recorded_priority_requires_all_files_to_be_live(self):
        query = ('option', '**/test*domain*.py')
        hits = [('z.py', 2), ('a.py', 1), ('z.py', 4)]
        with patch('replay.search_order.table', return_value={query:['a.py','missing.py']}):
            self.assertEqual(restore_search_order(iter(hits), *query), hits)
        with patch('replay.search_order.table', return_value={query:['a.py']}):
            self.assertEqual(restore_search_order(iter(hits), *query), [('a.py',1),('z.py',2),('z.py',4)])

    def test_live_search_preserves_matches_and_line_order(self):
        hits=[('z.py',2),('a.py',1),('z.py',4),('a.py',1),('b.py',7)]
        result=reorder(hits,['z.py'])
        self.assertEqual(collections.Counter(result),collections.Counter(hits))
        self.assertEqual(result[:2],[('z.py',2),('z.py',4)])
        self.assertEqual(result[2:],[('a.py',1),('a.py',1),('b.py',7)])
        with self.assertRaises(ValueError):reorder(hits,['missing.py'])

    def test_conflicting_recordings_do_not_guess(self):
        def step(files):
            return {'action':{'action_args_class':'moatless.actions.find_code_snippet.FindCodeSnippetArgs',
                              'code_snippet':'needle','file_pattern':'*.py'},
                    'observation':{'properties':{'search_hits':{'files':[{'file_path':p} for p in files]}}}}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'trace.json';p.write_text(json.dumps({'root':{'action_steps':[step(['a.py']),step(['b.py'])]}}))
            table=build_table(p)
            self.assertEqual(table['orders'],[])
            self.assertEqual(table['ambiguous_queries_passthrough'],1)

    def test_repaired_diff_is_bound_to_trace_transition_and_original_bytes(self):
        import hashlib
        sha=lambda text:hashlib.sha256(text.encode()).hexdigest()
        r=dict(trace_sha256='a'*64,instance='repo-1',transition=7,
               original_diff_sha256=sha('broken'),repaired_diff='fixed',
               repaired_diff_sha256=sha('fixed'),before_sha256='b'*64,after_sha256='c'*64,
               path='file.py',pre_snapshot_sha256='d'*64,post_snapshot_sha256='e'*64,
               corroborating_successor_ids=[8,9])
        args=dict(trace_sha256='a'*64,instance='repo-1',transition_id=7,repairs=[r])
        op=recorded_diff_op('broken',**args)
        self.assertEqual(op['diff'],'fixed')
        self.assertEqual(op['original_diff'],'broken')
        for change in [dict(trace_sha256='f'*64),dict(instance='repo-2'),dict(transition_id=8)]:
            self.assertEqual(recorded_diff_op('broken',**dict(args,**change))['diff'],'broken')
        self.assertEqual(recorded_diff_op('different',**args)['diff'],'different')
        with self.assertRaises(ValueError):
            recorded_diff_op('broken',**dict(args,repairs=[dict(r,repaired_diff='tampered')]))
        with self.assertRaises(ValueError):
            recorded_diff_op('broken',**dict(args,repairs=[r,r]))

    def test_storage_proof_rejects_disk_swap_wrong_node(self):
        require_tmpfs({'fstype':'tmpfs','options':'rw,noswap,mpol=bind:0'},0)
        for fs,opts in [('xfs','rw,noswap,mpol=bind:0'),('tmpfs','rw,mpol=bind:0'),('tmpfs','rw,noswap,mpol=bind:2')]:
            with self.assertRaises(ValueError):require_tmpfs({'fstype':fs,'options':opts},0)
        self.assertEqual(cpuset('0-3,6'),{0,1,2,3,6})

    def test_deviation_audits_incremental_overhead_and_duplicates(self):
        def data(ratio):
            return {'experiments':{name:{'metrics':([dict(metric='ratio',value=ratio,unit='x',n=2)] if name=='figure-07' else [])}
                    for name in ('figure-01','figure-06','figure-07')}}
        rows=audit.changes(data(1.1),data(1.02))
        self.assertFalse(rows[0]['exceeds_50_percent'])
        self.assertTrue(rows[1]['exceeds_50_percent'])
        fresh=data(1.1);fresh['experiments']['figure-07']['metrics']*=2
        with self.assertRaises(ValueError):audit.changes(fresh,data(1.02))

class LongUnixSocket(unittest.TestCase):
    def test_real_http_request_under_long_worktree_path(self):
        import subprocess,time
        from runners.vm import fc_put
        code = """import socket
s=socket.socket(socket.AF_UNIX)
s.bind('api.sock')
s.listen(1)
c,_=s.accept()
data=c.recv(65536)
assert data.startswith(b'PUT /machine-config ')
c.sendall(b'HTTP/1.1 204 No Content\\r\\nContent-Length: 0\\r\\n\\r\\n')
c.close()
s.close()
"""
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/('long-worktree-'*12);directory.mkdir()
            server=subprocess.Popen([sys.executable,'-c',code],cwd=directory)
            try:
                for _ in range(100):
                    if (directory/'api.sock').exists():break
                    time.sleep(.01)
                self.assertGreater(len(str(directory/'api.sock')),108)
                fc_put(directory/'api.sock','machine-config',{'vcpu_count':4})
                self.assertEqual(server.wait(timeout=5),0)
            finally:
                if server.poll() is None:server.kill();server.wait()
