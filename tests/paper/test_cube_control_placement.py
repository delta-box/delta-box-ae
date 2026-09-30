"""Kernel-mask restoration tests; service calls and privileged paths use fixtures."""
import ast
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'ae/scripts/cube_control_context.py'
if not SOURCE.exists():
    SOURCE = ROOT.parent / 'launcher-candidate/ae/scripts/cube_control_context.py'
FIELDS = ('cpuset.cpus', 'cpuset.mems', 'cpuset.cpus.effective', 'cpuset.mems.effective')
UNITS = ['cube-sandbox-cube-api.service', 'cube-sandbox-cubemaster.service', 'cube-sandbox-cubelet.service']


def actual_control_functions():
    tree = ast.parse(SOURCE.read_text())
    names = {'save', 'restore_masks', 'service_cgroup_masks', 'restore_service_cgroup_masks', 'placement'}
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = types.ModuleType('isolated_cube_control')
    module.__dict__.update(contextmanager=contextmanager, Path=Path, subprocess=subprocess, json=json,
                           UNITS=UNITS, CONTAINERS=[],
                           UNIT_PROPERTIES=('AllowedCPUs', 'AllowedMemoryNodes', 'CPUAffinity', 'NUMAPolicy', 'NUMAMask'))
    exec(compile(tree, str(SOURCE), 'exec'), module.__dict__)
    return module


class MaskFixture:
    """Simulate inherited kernel masks and systemd's no-op empty property write."""
    def __init__(self, root):
        self.root = root
        self.cgroups = root / 'cgroup'
        self.systemd = root / 'systemd'
        self.commands = []
        self.writes = []
        self.bad_effective = set()
        self.property_state = {unit: {key: '' for key in
            ('AllowedCPUs', 'AllowedMemoryNodes', 'CPUAffinity', 'NUMAPolicy', 'NUMAMask')} for unit in UNITS}
        for unit in UNITS:
            group = self.group(unit)
            group.mkdir(parents=True)
            for field in FIELDS:
                (group / field).write_text('\n')
        sandbox = self.cgroups / 'cube_sandbox'
        sandbox.mkdir()
        (sandbox / 'cpuset.cpus').write_text('0-95\n')
        (sandbox / 'cpuset.mems').write_text('0-5\n')
        self.real_read = Path.read_text
        self.real_write = Path.write_text

    def group(self, unit):
        return self.cgroups / 'system.slice' / unit

    def path(self, value):
        path = Path(value)
        for original, target in [('/sys/fs/cgroup', self.cgroups), ('/run/systemd/system', self.systemd)]:
            if path == Path(original) or path.is_relative_to(original):
                return target / path.relative_to(original)
        return path

    def read(self, path, *args, **kwargs):
        if path.is_relative_to(self.cgroups) and path.name.endswith('.effective'):
            raw = self.real_read(path.with_name(path.name.removesuffix('.effective'))).strip()
            if path.parent.name in self.bad_effective and path.name == 'cpuset.mems.effective':
                return '0\n'
            return (raw or ('0-95' if path.name == 'cpuset.cpus.effective' else '0-5')) + '\n'
        return self.real_read(path, *args, **kwargs)

    def write(self, path, value, *args, **kwargs):
        if path.is_relative_to(self.cgroups):
            self.writes.append((path, value))
        return self.real_write(path, value, *args, **kwargs)

    def output(self, *command):
        if command[:2] == ('systemctl', 'is-active'):
            return 'active'
        if command[:2] == ('systemctl', 'show'):
            unit = command[2]
            key = command[command.index('-p') + 1]
            if key == 'ControlGroup':
                return '/system.slice/' + unit
            return self.property_state[unit][key]
        raise AssertionError('Unexpected external command: ' + repr(command))

    def run(self, *command, **kwargs):
        self.commands.append(command)
        if command[:2] == ('systemctl', 'set-property'):
            unit = command[3]
            for argument in command[4:]:
                key, value = argument.split('=', 1)
                self.property_state[unit][key] = value
                field = {'AllowedCPUs': 'cpuset.cpus', 'AllowedMemoryNodes': 'cpuset.mems'}[key]
                # The regression: an empty resource property leaves the kernel
                # mask from the measurement in place until explicitly cleared.
                if value:
                    (self.group(unit) / field).write_text(value + '\n')
        elif command != ('systemctl', 'daemon-reload'):
            raise AssertionError('Unexpected external command: ' + repr(command))


class CgroupMaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = MaskFixture(self.root)
        self.control = actual_control_functions()
        self.control.Path = self.fixture.path
        self.control.output = self.fixture.output
        self.control.run = self.fixture.run
        self.patches = [mock.patch.object(Path, 'read_text', lambda path, *a, **k: self.fixture.read(path, *a, **k)),
                        mock.patch.object(Path, 'write_text', lambda path, value, *a, **k: self.fixture.write(path, value, *a, **k))]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_capture_distinguishes_empty_raw_masks_from_inherited_effective_masks(self):
        masks = self.control.service_cgroup_masks(UNITS[0])
        self.assertEqual(masks, {'path': str(self.fixture.group(UNITS[0])),
            'cpuset.cpus': '', 'cpuset.mems': '', 'cpuset.cpus.effective': '0-95', 'cpuset.mems.effective': '0-5'})

    def test_capture_rejects_wrong_relative_traversal_and_unit_identity(self):
        for path in ('system.slice/' + UNITS[0], '/system.slice/../' + UNITS[0], '/system.slice/other.service'):
            with self.subTest(path=path), mock.patch.object(self.control, 'output', return_value=path):
                with self.assertRaisesRegex(RuntimeError, 'Unexpected Cube service cgroup'):
                    self.control.service_cgroup_masks(UNITS[0])

    def test_restore_clears_empty_raw_masks_memory_before_cpu_and_checks_effective(self):
        unit = UNITS[0]
        expected = self.control.service_cgroup_masks(unit)
        self.fixture.run('systemctl', 'set-property', '--runtime', unit, 'AllowedCPUs=0-3', 'AllowedMemoryNodes=0')
        self.fixture.run('systemctl', 'set-property', '--runtime', unit, 'AllowedCPUs=', 'AllowedMemoryNodes=')
        self.assertEqual(self.control.service_cgroup_masks(unit)['cpuset.mems'], '0')
        self.fixture.writes.clear()
        self.assertEqual(self.control.restore_service_cgroup_masks(unit, expected), expected)
        self.assertEqual(self.fixture.writes, [(self.fixture.group(unit) / 'cpuset.mems', '\n'),
                                               (self.fixture.group(unit) / 'cpuset.cpus', '\n')])

    def test_restore_rejects_changed_cgroup_identity_before_writing(self):
        expected = self.control.service_cgroup_masks(UNITS[0])
        expected['path'] = str(self.fixture.group(UNITS[1]))
        self.fixture.writes.clear()
        with self.assertRaisesRegex(RuntimeError, 'cgroup identity changed'):
            self.control.restore_service_cgroup_masks(UNITS[0], expected)
        self.assertEqual(self.fixture.writes, [])

    def test_restore_preserves_nonempty_prior_raw_masks_exactly(self):
        unit = UNITS[0]
        group = self.fixture.group(unit)
        (group / 'cpuset.cpus').write_text('28-31\n')
        (group / 'cpuset.mems').write_text('1\n')
        expected = self.control.service_cgroup_masks(unit)
        self.fixture.run('systemctl', 'set-property', '--runtime', unit, 'AllowedCPUs=0-3', 'AllowedMemoryNodes=0')
        self.fixture.writes.clear()
        self.assertEqual(self.control.restore_service_cgroup_masks(unit, expected), expected)
        self.assertEqual(self.fixture.writes, [(group / 'cpuset.mems', '1\n'), (group / 'cpuset.cpus', '28-31\n')])

    def test_restore_rejects_effective_masks_that_stay_pinned(self):
        expected = self.control.service_cgroup_masks(UNITS[0])
        self.fixture.bad_effective.add(UNITS[0])
        with self.assertRaisesRegex(RuntimeError, 'effective cgroup masks did not restore'):
            self.control.restore_service_cgroup_masks(UNITS[0], expected)

    def test_placement_finally_restores_actual_masks_after_empty_systemd_properties(self):
        out, guard = self.root / 'evidence', self.root / 'RECOVERY_REQUIRED.json'
        with mock.patch.object(self.control, 'restore_service_cgroup_masks',
                               wraps=self.control.restore_service_cgroup_masks) as restore:
            with self.control.placement(0, '0-3', out, guard):
                for unit in UNITS[:-1]:
                    self.assertEqual(self.control.service_cgroup_masks(unit)['cpuset.mems'], '0')
            before = json.loads((out / 'placement-before.json').read_text())
            self.assertEqual(restore.call_args_list, [mock.call(unit, before['unit_cgroups'][unit])
                                                     for unit in reversed(UNITS[:-1])])
        self.assertFalse(guard.exists())
        restored = json.loads((out / 'placement-restored.json').read_text())
        self.assertEqual(restored['errors'], [])
        for unit in UNITS[:-1]:
            self.assertEqual(restored['unit_cgroups'][unit], before['unit_cgroups'][unit])
            self.assertEqual(restored['units'][unit]['AllowedMemoryNodes'], '')
        self.assertEqual(self.control.service_cgroup_masks(UNITS[-1]), before['unit_cgroups'][UNITS[-1]])

    def test_placement_effective_restore_failure_keeps_guard_and_evidence(self):
        out, guard = self.root / 'evidence', self.root / 'RECOVERY_REQUIRED.json'
        with self.assertRaisesRegex(RuntimeError, 'placement restoration failed'):
            with self.control.placement(0, '0-3', out, guard):
                self.fixture.bad_effective.add(UNITS[0])
        self.assertTrue(guard.exists())
        evidence = json.loads((out / 'placement-restored.json').read_text())
        self.assertTrue(any('effective cgroup masks did not restore' in error for error in evidence['errors']))
        self.assertEqual(evidence['unit_cgroups'][UNITS[0]]['cpuset.mems.effective'], '0')

    def test_inner_recovery_guard_prevents_every_outer_restoration(self):
        out, guard = self.root / 'evidence', self.root / 'RECOVERY_REQUIRED.json'
        with mock.patch.object(self.control, 'restore_service_cgroup_masks',
                               wraps=self.control.restore_service_cgroup_masks) as restore:
            with self.assertRaisesRegex(RuntimeError, 'retained for resource recovery'):
                with self.control.placement(0, '0-3', out, guard):
                    guard.write_text('{"inner_cleanup_failed":true}')
                    self.fixture.commands.clear()
            restore.assert_not_called()
        self.assertEqual(self.fixture.commands, [])
        self.assertTrue(guard.exists() and (out / 'placement-retained.json').exists())
        self.assertEqual(self.control.service_cgroup_masks(UNITS[0])['cpuset.mems'], '0')


if __name__ == '__main__':
    unittest.main()
