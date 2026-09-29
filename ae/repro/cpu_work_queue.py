"""Durable per-experiment queue; current-run resume only, with fixed node leases."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import time
from .common import file_record, load_config, write_json
from .result_storage import run_lock, no_symlink_parents


def service_group(name):
    return name.endswith(('-cube', '-e2b'))


def permits_scope_expansion(previous, selected, all_cpu):
    # A queue worker considers the complete CPU pool. It may add candidates to
    # its old lane selection, but cannot remove a previously selected group.
    return (list(selected) == list(all_cpu) and bool(previous)
            and set(previous).issubset(selected))


def recover_groups(output, experiments, placement, config_path, verify_images):
    from .analysis import Evidence, FreshRun
    output = no_symlink_parents(output)
    groups = {name: {'status': 'pending', 'node': None} for name in experiments}
    expected_source = file_record(config_path)
    for node, cpus in placement.items():
        lane = output / 'lanes' / ('numa' + str(node))
        review_path = lane / 'review.json'
        if not review_path.exists():
            continue
        previous = json.loads(review_path.read_text())
        request = previous.get('measurement_request', {})
        if request.get('node') != node or request.get('cpus') != cpus:
            raise ValueError('Queue resume cannot change a lane measurement binding')
        rows = previous.get('coverage', [])
        for row in rows:
            name = row['experiment']
            if name not in groups:
                raise ValueError('Unexpected prior experiment in queue resume')
            if row['status'] == 'not-run':
                continue
            if groups[name]['node'] is not None:
                raise ValueError('Experiment was measured in more than one lane')
            groups[name]['node'] = node  # Partial groups keep their original node.
            if row['status'] != 'ok':
                continue
            if row.get('config_source') != expected_source:
                raise ValueError('Completed group source configuration changed: ' + name)
            effective = row.get('effective_config')
            if not effective or file_record(Path(effective['path'])) != effective:
                raise ValueError('Completed effective configuration changed: ' + name)
            root = lane / 'runs' / name
            plan = json.loads((root / 'suite.json').read_text())
            jobs = plan.get('jobs', [])
            if (plan.get('status') != 'ok' or not jobs
                    or any(j.get('status') != 'ok' for j in jobs)
                    or row.get('successful_jobs') != len(jobs)
                    or row.get('planned_jobs') != len(jobs)):
                raise ValueError('Completed group lacks complete successful jobs: ' + name)
            ev, claimed = Evidence(root, 'fresh'), set()
            for job in jobs:
                manifests = sorted((root / job['key']).glob('**/run.json'))
                if not manifests:
                    raise ValueError('Completed job has no producer manifest: ' + job['key'])
                for manifest in manifests:
                    fresh = FreshRun(ev, manifest, claimed)
                    if fresh.config['experiment'] != name:
                        raise ValueError('Producer experiment differs from queue group')
                    verify_images(fresh.config, cache=output / '.queue-resume-image-hashes.json')
            groups[name].update(status='ok', row=copy.deepcopy(row),
                                validated_review=file_record(review_path),
                                validated_suite=file_record(root / 'suite.json'))
    return groups


class WorkQueue:
    def __init__(self, path, node):
        self.path = Path(path)
        self.node = node
        self.lock = self.path.with_suffix('.lock')

    @classmethod
    def initialize(cls, path, experiments, placement, *, groups=None):
        path = no_symlink_parents(path)
        state = {'schema_version': 1, 'experiments': list(experiments),
                 'placement': {str(k): v for k, v in placement.items()},
                 'groups': groups or {n: {'status': 'pending', 'node': None} for n in experiments}}
        write_json(path, state)
        return state

    def _read(self):
        state = json.loads(self.path.read_text())
        if state.get('schema_version') != 1 or str(self.node) not in state['placement']:
            raise ValueError('Invalid CPU work queue')
        return state

    def completed_rows(self):
        with run_lock(self.lock, wait=True):
            state = self._read()
            return [copy.deepcopy(state['groups'][n]['row']) for n in state['experiments']
                    if state['groups'][n]['status'] == 'ok' and state['groups'][n]['node'] == self.node]

    def claim(self):
        with run_lock(self.lock, wait=True):
            state = self._read()
            rows = state['groups']
            if any(v['status'] == 'failed' for v in rows.values()):
                return 'done', None
            if any(v['status'] == 'running' and v['node'] == self.node for v in rows.values()):
                raise ValueError('A node cannot claim two experiments concurrently')
            service_busy = any(service_group(n) and v['status'] == 'running' for n, v in rows.items())
            for name in state['experiments']:
                row = rows[name]
                if row['status'] != 'pending' or row['node'] not in (None, self.node):
                    continue
                if service_group(name) and service_busy:
                    continue
                row.update(status='running', node=self.node)
                write_json(self.path, state)
                return 'claimed', name
            eligible = any(v['status'] == 'pending' and v['node'] in (None, self.node) for v in rows.values())
            return ('wait', None) if eligible else ('done', None)

    def work(self):
        while True:
            status, name = self.claim()
            if status == 'done':
                return
            if status == 'wait':
                time.sleep(0.25)
                continue
            yield name

    def finish(self, name, row):
        with run_lock(self.lock, wait=True):
            state = self._read()
            assigned = state['groups'][name]
            if assigned['status'] != 'running' or assigned['node'] != self.node:
                raise ValueError('Experiment completion does not own its queue claim')
            assigned.update(status='ok' if row.get('status') == 'ok' else 'failed', row=copy.deepcopy(row))
            write_json(self.path, state)
