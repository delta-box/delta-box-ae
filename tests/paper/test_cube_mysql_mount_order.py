"""Container restoration compares mount content independently of Docker list order."""
import ast
import copy
import json
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'ae/scripts/cube_control_context.py'

class MysqlMountOrderTest(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text())
        tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'verify_mysql_container']
        self.before = {'running': True, 'id': 'original', 'image_id': 'sha256:original',
            'config_sha256': 'original-config', 'effective_cpus': '0-95', 'effective_mems': '0-5',
            'mounts': [{'Type': 'bind', 'Source': '/original/sql', 'Destination': '/docker-entrypoint-initdb.d', 'RW': False, 'Mode': 'ro', 'Propagation': 'rprivate'},
                       {'Type': 'volume', 'Name': 'original-data', 'Source': '/original/data', 'Destination': '/var/lib/mysql', 'RW': True, 'Driver': 'local', 'Mode': 'rw', 'Propagation': ''}]}
        self.after = copy.deepcopy(self.before)
        ns = {'json': json, 'MYSQL': 'mysql', 'inspect': lambda _: self.after}
        exec(compile(tree, str(SOURCE), 'exec'), ns)
        self.verify = ns['verify_mysql_container']

    def test_recreated_container_accepts_reordered_identical_mounts(self):
        self.after['id'] = 'recreated'
        self.after['mounts'].reverse()
        self.assertIs(self.verify(self.before, same_id=False), self.after)

    def test_reordered_dictionary_keys_preserve_identical_mounts(self):
        self.after['mounts'] = [dict(reversed(list(m.items()))) for m in reversed(self.after['mounts'])]
        self.verify(self.before, same_id=True)

    def test_every_mount_field_difference_is_rejected(self):
        for i, mount in enumerate(self.before['mounts']):
            for key, value in mount.items():
                with self.subTest(index=i, key=key):
                    self.after = copy.deepcopy(self.before)
                    self.after['mounts'][i][key] = not value if type(value) is bool else str(value) + '-changed'
                    with self.assertRaises(RuntimeError): self.verify(self.before, same_id=False)

    def test_missing_extra_or_duplicated_mount_is_rejected(self):
        mounts = self.before['mounts']
        for replacement in (mounts[:1], mounts + mounts[:1], [mounts[0], mounts[0]]):
            with self.subTest(mounts=replacement):
                self.after = copy.deepcopy(self.before); self.after['mounts'] = replacement
                with self.assertRaises(RuntimeError): self.verify(self.before, same_id=False)

    def test_image_configuration_placement_and_running_remain_strict(self):
        for key in ('image_id', 'config_sha256', 'effective_cpus', 'effective_mems', 'running'):
            with self.subTest(key=key):
                self.after = copy.deepcopy(self.before); self.after[key] = False if key == 'running' else 'changed'
                with self.assertRaises(RuntimeError): self.verify(self.before, same_id=False)

    def test_same_id_contract_still_rejects_recreation(self):
        self.after['id'] = 'recreated'; self.after['mounts'].reverse()
        with self.assertRaises(RuntimeError): self.verify(self.before, same_id=True)

    def test_verification_does_not_mutate_evidence(self):
        self.after['mounts'].reverse(); snapshot = copy.deepcopy((self.before, self.after))
        self.verify(self.before, same_id=True)
        self.assertEqual((self.before, self.after), snapshot)

if __name__ == '__main__': unittest.main()
