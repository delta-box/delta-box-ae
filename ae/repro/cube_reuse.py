"""Explicit Cube paper-disk result imports; old measurements retain their bytes/source."""
from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
import os
import re
import stat
from pathlib import Path
import shutil
import subprocess
import tarfile

from ae.repro.common import file_record, number, write_json
from ae.repro.figure09_reuse import (
    _arg, _bound_json, _normalized_command, _plain_path, _sha,
    EMPTY_SHA256, MIN_FREE_BYTES, measurement_fingerprint,
)

EXPERIMENT = 'table-02-cube'
KIND = 'verified-cube-paper-disk-job-reuse'
TEMPLATE = 'cube-official-blog-sandbox-code-2c2g-20260609'
TEMPLATE_SHA = '99905903fe27ee60fcc51d0863ad55f51b7cab657959523aade6e59b83231840'


def require(condition, message):
    if not condition:
        raise ValueError('Cube reuse refused: ' + message)


def _tree_records(root):
    # Only the producer's two immutable dependency links may survive staging.
    # Evidence and result paths themselves must always be plain files.
    links = {'payload/moatless-det-src', 'payload/index_store'}
    records = {}
    for path in sorted(Path(root).rglob('*')):
        name = str(path.relative_to(root))
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            require(name in links and path.resolve(strict=True).is_dir(), 'unexpected evidence/dependency link: ' + name)
            records[name] = {'symlink': os.readlink(path), 'resolved': str(path.resolve()), 'bytes': 0}
        elif stat.S_ISREG(mode):
            before = path.stat()
            digest = _sha(path)
            after = path.stat()
            require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'file changed while hashing')
            records[name] = {'sha256': digest, 'bytes': before.st_size}
        else:
            require(stat.S_ISDIR(mode), 'non-regular entry: ' + name)
    return records


def verify_imported_job(job, suite):
    descriptor = suite.get('reuse_manifest') or {}
    imported, actual = _bound_json(descriptor.get('path', ''))
    require(actual == descriptor and imported.get('status') == 'copied-and-verified' and
            imported.get('kind') == KIND, 'reuse manifest changed/incomplete')
    receipts = [r for r in imported['jobs'] if r['job'] == job['key']]
    require(len(receipts) == 1, 'reuse receipt missing/duplicate')
    receipt = receipts[0]
    root = Path(_arg(job['command'], '--out'))
    origin_path = root / 'reuse-origin.json'
    expected = dict(kind=KIND, original_root=str(Path(receipt['run']['path']).parent),
                    original_review=imported['original_review'], original_suite=imported['original_suite'],
                    planner_release=imported['planner_release'],
                    measurement_fingerprint_sha256=imported['measurement_fingerprint']['sha256'], **receipt)
    require(job.get('reuse_origin') == str(origin_path) and json.loads(origin_path.read_text()) == expected,
            'reuse origin changed')
    records = _tree_records(root)
    records.pop('reuse-origin.json', None)
    require(records == receipt['files'], 'imported bytes changed')
    run, record = _bound_json(root / 'run.json')
    require(record['sha256'] == receipt['run']['sha256'] and record['bytes'] == receipt['run']['bytes'] and
            run['release'] == receipt['release'] == job.get('measurement_release') and
            job.get('original_run') == receipt['run'], 'imported measurement identity changed')
    return receipt


def bound(root, item, *, within=None):
    path = Path(item['path'])
    if not path.is_absolute():
        path = Path(root) / path
    if within is not None:
        path = _plain_path(path, within)
    actual = file_record(path)
    require(actual['sha256'] == item['sha256'] and actual['bytes'] == item['bytes'],
            'bound artifact changed: ' + str(path))
    return path


def source_commit_identity(repo, release):
    """Retain the measured working-tree identity without requiring a clean commit."""
    return dict(release, source_policy='record-only')


def validate_pilot(instance, pilot, schedule, schedule_sha):
    require(pilot.get('ok') is True and pilot.get('status') == 'ok', 'pilot not successful')
    require(pilot.get('instance') == instance and pilot.get('schedule_sha256') == schedule_sha, 'pilot input identity')
    require(pilot.get('warm_action_worker') is True and pilot.get('llm_replay_mode') == 'schedule_latency_sleep',
            'action-worker/RTT method differs')
    require(pilot.get('template') == TEMPLATE and pilot.get('measurement_scope') ==
            'host-side CubeSandbox SDK API elapsed time', 'template or timing method differs')
    events = pilot.get('iterations', [])
    require(bool(schedule) and len(events) == len(schedule), 'incomplete event set')
    counter = Counter()
    for index, (expected, event) in enumerate(zip(schedule, events)):
        label = f'{instance}/{index}'
        kind = expected['type']
        require(kind in ('ckpt', 'restore') and event.get('ok') is True and event.get('ev_i') == index
                and event.get('kind') == kind, label + ': event identity/status')
        require(event.get('agent_mode') == 'real' and event.get('require_real_agent') is True, label + ': not real')
        key = 'ckpt_id' if kind == 'ckpt' else 'restore_to_ckpt_id'
        require(event.get(key) == expected.get(key) and event.get('node_id') == expected.get('node_id'),
                label + ': changed target/node')
        number(event.get('checkpoint_wall_ms' if kind == 'ckpt' else 'restore_wall_ms'), 'API wall')
        counter[kind] += 1
        if kind == 'ckpt':
            ops = expected.get('worker_ops') or []
            response = event.get('action_response') or {}
            results = (response.get('event') or {}).get('worker_results') or []
            require(event.get('worker_ops_n') == len(ops) == len(results), label + ': missing/additional worker actions')
            counter['action_checkpoints' if ops else 'initial_without_actions'] += 1
            if ops:
                require(expected.get('worker_ops_required') is True and response.get('ok') is True and
                        response['event'].get('worker_ops_ok') is True, label + ': action response failure')
                require(event.get('cube_steps') and all(x.get('ok') is True for x in event['cube_steps']),
                        label + ': action step failure')
                require(event.get('action_step_idx') == ops[0]['action_step_idx'], label + ': action step differs')
            for op, result in zip(ops, results):
                require(result.get('ok') is True and op.get('type') == result.get('type') and
                        op.get('action_class') == result.get('action_class'), label + ': worker result differs')
                counter['worker_operations'] += 1
        else:
            steps = event.get('cube_steps') or []
            require(len(steps) == 1 and steps[0].get('ok') is True and
                    (steps[0].get('rollback_response') or {}).get('status') == 'READY', label + ': restore failed')
    require(pilot.get('n_schedule_events') == len(schedule) and pilot.get('n_ckpt_events') == counter['ckpt']
            and pilot.get('n_restore_events') == counter['restore'], 'pilot counters disagree')
    return dict(counter)


def validate_phases(root, run, pilot, counts):
    from ae.runners.cube_phases import parser, _strict_event
    capture_path = bound(root, run['phase_evidence']['phase_json'], within=root)
    log_path = bound(root, run['phase_evidence']['captured_log'], within=root)
    require(capture_path == root / 'cube_phases.json' and log_path == root / 'cubelet-phases.log',
            'unexpected phase evidence paths')
    capture = json.loads(capture_path.read_text())
    require(capture.get('strict_success_and_phase_contract') is True and
            run['phase_evidence'].get('strict') is True, 'non-strict phase evidence')
    require(bound(root, capture['pilot'], within=root) == bound(root, run['result'], within=root) and
            bound(root, capture['captured_log'], within=root) == log_path, 'phase/pilot binding differs')
    parsed = []
    for line_number, line in enumerate(log_path.read_text().splitlines(), 1):
        row = parser.parse_phase_line(line)
        if row:
            raw = json.loads(line)
            parsed.append({**row, 'success': raw.get('success'), 'raw_record': raw,
                           'raw_line': line, 'captured_line': line_number})
    expected_n = 3 * counts.get('ckpt', 0) + 10 * counts.get('restore', 0)
    require(capture.get('phase_record_count') == expected_n == len(capture.get('raw_phases', [])),
            'raw phase count differs')
    require(len(capture.get('events', [])) == len(pilot['iterations']), 'phase event count differs')
    accumulated = []
    for event, saved in zip(pilot['iterations'], capture['events']):
        kind = event['kind']
        api = event['snapshot'] if kind == 'ckpt' else event['cube_steps'][0]
        require(api.get('api_retries') in (0, []), 'API was submitted more than once')
        matched = [p for p in parsed if p['snapshot_id'] == event['snapshot_id'] and
                   p['sandbox_id'] == api['sandbox_id'] and
                   p['flow'] == ('commit_sandbox' if kind == 'ckpt' else 'rollback_sandbox') and
                   api['api_start_unix_ns'] <= p['start_unix_ns'] <= p['end_unix_ns'] <= api['api_end_unix_ns']]
        required = ({'rootfs_dump', 'memory_prepare', 'memory_dump'} if kind == 'ckpt' else
                    parser.RS_FS | parser.RS_META_CONTROL | {'rollback_total', 'shim_update_restore'})
        _strict_event(event, api, matched, required)
        indices = list(range(len(accumulated), len(accumulated) + len(matched)))
        accumulated.extend(matched)
        fs = parser.sum_phase(matched, parser.CK_FS if kind == 'ckpt' else parser.RS_FS)
        process = parser.sum_phase(matched, parser.CK_PROC if kind == 'ckpt' else parser.RS_PROC)
        wall = event['checkpoint_wall_ms' if kind == 'ckpt' else 'restore_wall_ms']
        union = parser.union_ms(matched)
        other = wall - union if kind == 'ckpt' else (
            parser.sum_phase(matched, parser.RS_META_CONTROL) + wall - parser.sum_phase(matched, {'rollback_total'}))
        expected = dict(event_index=event['ev_i'], kind=kind, snapshot_id=event['snapshot_id'],
                        sandbox_id=api['sandbox_id'], wall_ms=wall, api_start_unix_ns=api['api_start_unix_ns'],
                        api_end_unix_ns=api['api_end_unix_ns'], filesystem_ms=fs, process_ms=process,
                        other_ms=other, phase_union_ms=union, unclassified_ms=wall-fs-process-other,
                        phase_names=sorted(required), phase_record_indices=indices, success_evidence='explicit-json')
        require(saved == expected, 'saved phase values differ from raw intervals')
    require(accumulated == capture['raw_phases'] and len(accumulated) == expected_n and
            len({r['captured_line'] for r in accumulated}) == expected_n, 'raw phase JSON/log/indices differ')
    require(run['phase_evidence'].get('events') == len(pilot['iterations']) and
            run['phase_evidence'].get('raw_phase_records') == expected_n, 'run phase counts differ')
    return expected_n


def validate_disk(root, run, source, verify_images=None):
    before, after = [json.loads((root / name).read_text()) for name in ('cube_disk_before.json', 'cube_disk_after.json')]
    for proof in (before, after):
        require(proof.get('profile') == 'paper-disk' and proof.get('node') == 2 and
                proof.get('runner_cpus') == '48-51' and proof.get('service_cpus') == '48-71', 'disk placement differs')
        require(proof['paths']['/data/cubelet/storage']['fstype'] == 'xfs' and
                proof['workspace_disk']['mount']['fstype'] not in ('tmpfs', 'overlay', 'nfs', 'nfs4') and
                proof['workspace_disk']['physical_disks'], 'not disk-backed XFS')
        require(proof['capacity']['reserve_bytes'] >= MIN_FREE_BYTES and
                proof['capacity']['available_bytes'] >= proof['capacity']['required_bytes'], 'disk admission failed')
        manifest = json.loads(bound(root, proof['manifest'], within=source).read_text())
        require(manifest.get('profile') == 'paper-disk' and manifest.get('identity') == proof['identity'] and
                manifest.get('service_pid') == proof['service_pid'], 'storage manifest identity differs')
    for key in ('manifest', 'identity', 'service_pid', 'node', 'service_cpus', 'runner_cpus', 'paths', 'loop'):
        require(before[key] == after[key], 'disk identity changed during input: ' + key)
    require(run['disk_backing'] == before and run['disk_backing_after'] == after, 'disk proof not bound into run')
    env = run['cube_environment']
    require(env['template_cpu_millicores'] == 2000 and env['template_memory_mb'] == 2048 and
            env['template']['artifact_sha256'] == TEMPLATE_SHA, 'template size/image differs')
    template = env['template']
    artifact_id = template.get('artifact_id')
    require(isinstance(artifact_id, str) and re.fullmatch(r'rfs-[0-9a-f]+', artifact_id),
            'invalid template artifact identity')
    image = dict(path=str(Path('/data/CubeMaster/storage') / artifact_id / (artifact_id + '.ext4')),
                 sha256=template['artifact_sha256'], bytes=int(template['artifact_size_bytes']))
    if verify_images is not None:
        verify_images({'images': {'cube_template': image}})
    else:
        bound(root, image)
    return {'identity': before['identity'], 'storage_manifest': before['manifest'],
            'template': template, 'actual_template_image': image,
            'phase_instrumentation': run['phase_instrumentation']}


def validate_job(prior, job, old_suite, source, *, repo, verify_images=None, claimed=None):
    require(prior.get('experiment') == job.get('experiment') == EXPERIMENT and prior.get('status') == 'ok',
            'candidate is not successful Cube')
    require(prior.get('run_purpose') == job.get('run_purpose') == 'full-cohort', 'partial scope')
    require(_normalized_command(prior['command']) == _normalized_command(job['command']) and
            prior.get('inputs') == job.get('inputs'), 'measurement command/input changed')
    require('--collect-phases' in prior['command'] and _arg(prior['command'], '--backend') == 'cube' and
            _arg(prior['command'], '--experiment-id') == EXPERIMENT, 'missing explicit Cube phase identity')
    root = _plain_path(_arg(prior['command'], '--out'), source)
    require(root == source / 'runs' / EXPERIMENT / job['key'], 'unexpected original job location')
    require(not (root / 'reuse-origin.json').exists(), 'chained imports require explicit original source')
    files = _tree_records(root)
    process_path = _plain_path(prior['process_manifest'], source)
    process, process_record = _bound_json(process_path)
    require(process.get('status') == 'ok' and process.get('returncode') == 0 and process.get('finished_at')
            and process.get('command') == prior['command'], 'outer process lacks successful terminal state')
    require(prior.get('staging_cleanup', {}).get('status') in ('ok', 'not-applicable'), 'cleanup failed')
    inner = json.loads((root / 'process/process.json').read_text())
    require(inner.get('status') == 'ok' and inner.get('returncode') == 0 and inner.get('finished_at'),
            'driver process lacks successful terminal state')
    run, run_record = _bound_json(root / 'run.json')
    instance = _arg(job['command'], '--instance')
    require(run.get('status') == 'ok' and run.get('experiment') == EXPERIMENT and run.get('backend') == 'cube'
            and run.get('instance') == instance and run.get('analysis_mode') == 'fresh-measurement' and
            run.get('run_purpose') == 'full-cohort', 'run identity/status differs')
    require(run.get('release') == old_suite['release'] and run.get('runtime', {}).get('commit') ==
            old_suite['release']['source_commit'] and run['runtime'].get('status') == '' and
            run['runtime'].get('tracked_diff_sha256') == EMPTY_SHA256, 'original source not clean/bound')
    require(run.get('storage_mode') == 'disk-backed-xfs' and
            run.get('measurement_identity') == old_suite.get('measurement_identity'), 'run resource identity differs')
    trace = Path(_arg(job['command'], '--trace')).resolve(strict=True)
    schedule = Path(_arg(job['command'], '--schedule')).resolve(strict=True)
    require(file_record(trace)['sha256'] == run['input']['sha256'] and
            file_record(trace)['bytes'] == run['input']['bytes'], 'trace bytes changed')
    require(file_record(schedule)['sha256'] == run['schedule']['sha256'] and
            file_record(schedule)['bytes'] == run['schedule']['bytes'], 'schedule bytes changed')
    bound(root, run['input'])
    bound(root, run['schedule'])
    require(bool(run.get('sources')), 'missing payload/driver source bindings')
    for item in run['sources']:
        bound(root, item)
    artifacts = run.get('artifacts', [])
    require(len({a['path'] for a in artifacts}) == len(artifacts), 'duplicate artifact paths')
    require({'cube_disk_before.json', 'cube_disk_after.json', 'cubelet-phases.log', 'cube_phases.json'} <=
            {a['path'] for a in artifacts}, 'missing disk/phase artifacts')
    for item in artifacts:
        bound(root, item, within=root)
    pilot_path = bound(root, run['result'], within=root)
    require(run['result'] in artifacts, 'pilot not a bound artifact')
    pilot = json.loads(pilot_path.read_text())
    counts = validate_pilot(instance, pilot, [json.loads(line) for line in schedule.read_text().splitlines() if line.strip()],
                            run['schedule']['sha256'])
    require(run['counts'] == {'checkpoints': counts.get('ckpt', 0), 'restores': counts.get('restore', 0)},
            'run count mismatch')
    phase_count = validate_phases(root, run, pilot, counts)
    environment = validate_disk(root, run, source, verify_images)
    require(_tree_records(root) == files and file_record(process_path) == process_record,
            'candidate changed during validation')
    return root, dict(job=job['key'], instance=instance, release=run['release'], run=run_record,
                      process=process_record, input=file_record(trace), schedule=file_record(schedule),
                      counts=counts, raw_phase_records=phase_count, environment=environment, files=files)


def prepare_reuse(plan, source, destination, *, repo, verify_images=None, check_active, validate_only=False):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    require(source.resolve(strict=True) == source and not destination.is_relative_to(source) and
            not source.is_relative_to(destination), 'source/destination must be disjoint plain paths')
    review_path, suite_path = source / 'review.json', source / 'runs' / EXPERIMENT / 'suite.json'
    review, review_record = _bound_json(review_path)
    old, suite_record = _bound_json(suite_path)
    require(review.get('status') in ('ok', 'failed', 'interrupted') and review.get('finished_at') and
            old.get('status') in ('ok', 'failed', 'interrupted'), 'source is not terminal')
    require(review.get('release') == old.get('release'), 'source review/suite release differs')
    require(plan.get('experiments') == old.get('experiments') == [EXPERIMENT] and
            plan.get('measurement_identity', {}).get('cube_profile') == 'paper-disk', 'Cube paper-disk only')
    require(old.get('effective_config_sha256') == plan.get('effective_config_sha256'), 'effective config changed')
    require(old.get('measurement_identity') == plan.get('measurement_identity'), 'resource policy changed')
    require(old.get('run_purpose') == plan.get('run_purpose') == 'full-cohort', 'partial scope')
    require(len(plan['jobs']) == len(old['jobs']) == 12 and
            len({j['key'] for j in old['jobs']}) == 12 and
            {j['key'] for j in plan['jobs']} == {j['key'] for j in old['jobs']}, 'requires complete original 12-job plan')
    require(not check_active(source), 'source has active file/mapping/mount references')
    service_path = _plain_path(review['cube_disk_service']['path'], source)
    bound(source, review['cube_disk_service'], within=source)
    restored_path = service_path.parent / 'restored.json'
    restored, restored_record = _bound_json(restored_path)
    require(restored.get('ActiveState') == 'active' and restored.get('override_removed') is True and
            restored.get('cleanup_errors') == [] and restored.get('readiness', {}).get('ready') is True and
            restored.get('readiness', {}).get('idle') is True, 'old private service did not restore cleanly')
    original_source = source_commit_identity(repo, old['release'])
    fingerprint = measurement_fingerprint(repo, old['release']['source_commit'])
    original = {j['key']: j for j in old['jobs']}
    candidates = [ (job, *validate_job(original[job['key']], job, old, source, repo=repo, verify_images=verify_images))
                  for job in plan['jobs'] if original[job['key']].get('status') == 'ok' ]
    require(bool(candidates), 'no completed matching jobs to reuse')
    total = sum(item['bytes'] for _, _, receipt in candidates for item in receipt['files'].values())
    ancestor = destination
    while not ancestor.exists():
        ancestor = ancestor.parent
    require(shutil.disk_usage(ancestor).free >= total + MIN_FREE_BYTES, '10 GiB reserve would be violated')
    origin = dict(schema_version=1, kind=KIND, status='validated', original_review=review_record,
                  original_suite=suite_record, original_release=old['release'], planner_release=plan['release'],
                  source_commit_identity=original_source, original_restoration=restored_record,
                  original_storage=review['cube_disk_service'], measurement_fingerprint=fingerprint,
                  measurement_identity=plan['measurement_identity'], copied_bytes=total,
                  reused_jobs=len(candidates), selected_jobs=12,
                  analysis_policy='Original and new measurement sources remain separate populations; aggregate only with an explicit per-instance source manifest.',
                  jobs=[r for _, _, r in candidates])
    for path, expected in [(review_path, review_record), (suite_path, suite_record), (restored_path, restored_record),
                           (service_path, review['cube_disk_service'])]:
        require(file_record(path) == expected, 'source control changed during validation')
    require(not check_active(source), 'source acquired active references during validation')
    if validate_only:
        return origin
    require(not destination.exists(), 'destination stage already exists')
    destination.mkdir(parents=True)
    for job, root, receipt in candidates:
        target = destination / job['key']
        require(_tree_records(root) == receipt['files'], 'source changed before copy')
        shutil.copytree(root, target, copy_function=shutil.copy2, symlinks=True)
        require(_tree_records(root) == receipt['files'] == _tree_records(target), 'copy/source bytes differ')
        write_json(target / 'reuse-origin.json', dict(kind=KIND, original_root=str(root),
            original_review=review_record, original_suite=suite_record, planner_release=origin['planner_release'],
            measurement_fingerprint_sha256=fingerprint['sha256'], **receipt))
        job.update(status='ok', reused_verified=True, execution='copied-completed-measurement',
                   measurement_release=receipt['release'], original_run=receipt['run'],
                   process_manifest=receipt['process']['path'], reuse_origin=str(target / 'reuse-origin.json'))
    for job, root, receipt in candidates:
        target_records = _tree_records(destination / job['key'])
        target_records.pop('reuse-origin.json', None)
        require(_tree_records(root) == receipt['files'] == target_records,
                'source or imported files changed before publishing receipt')
    for path, expected in [(review_path, review_record), (suite_path, suite_record), (restored_path, restored_record),
                           (service_path, review['cube_disk_service'])]:
        require(file_record(path) == expected, 'source control changed during copy')
    require(not check_active(source), 'source acquired active references during copy')
    origin['status'] = 'copied-and-verified'
    write_json(destination / 'reuse-manifest.json', origin)
    plan.update(import_verified=True, measurement_sources=[old['release'], plan['release']],
                reuse_manifest=file_record(destination / 'reuse-manifest.json'))
    return origin
