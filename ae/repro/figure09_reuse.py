"""Verify original Figure 9 results while recording editable source identities."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile

from ae.repro.common import file_record, write_json, number

# Record the current measurement code independently of the original results.
# Original payload bytes are verified from their retained source archives.
MEASUREMENT_PATHS = (
    'ae/runners', 'ae/vendor', 'backends', 'replay', 'pycriu',
    'ae/repro/common.py', 'ae/repro/process.py', 'ae/repro/repositories.py',
    'ae/scripts/run_pinned_measurement.py', 'release/lock.py',
)
MIN_FREE_BYTES = 10 * 1024**3
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _check(condition, message):
    if not condition:
        raise ValueError('Figure 9 reuse refused: ' + message)


def _arg(command, flag):
    _check(command.count(flag) == 1, 'missing/duplicate command option ' + flag)
    return command[command.index(flag) + 1]


def _normalized_command(command):
    command = list(command)
    for flag in ('--out', '--config'):
        command[command.index(flag) + 1] = '<' + flag[2:] + '>'
    return command


def _plain_path(path, root):
    path, root = Path(path).absolute(), Path(root).resolve(strict=True)
    _check(path.is_relative_to(root), 'path outside original run: ' + str(path))
    _check(path.resolve(strict=True) == path, 'symlink in original path: ' + str(path))
    return path


def _tree_records(root):
    """Hash complete copied bytes; refuse links, special files and mutable input."""
    records = {}
    for path in sorted(Path(root).rglob('*')):
        mode = path.lstat().st_mode
        _check(stat.S_ISDIR(mode) or stat.S_ISREG(mode), 'non-regular job entry: ' + str(path))
        if stat.S_ISREG(mode):
            before = path.stat()
            digest = _sha(path)
            after = path.stat()
            _check((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                   (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'job changed while hashing')
            records[str(path.relative_to(root))] = {'sha256': digest, 'bytes': before.st_size}
    return records


def measurement_fingerprint(repo, commit):
    # Source identities are descriptive; they do not admit or reject reuse.
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True)
    paths = git('ls-files', '--cached', '--others', '--exclude-standard', '--',
                *MEASUREMENT_PATHS).splitlines()
    records = {}
    for name in sorted(set(paths)):
        path = Path(repo) / name
        if path.is_symlink():
            records[name] = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        elif path.is_file():
            records[name] = _sha(path)
        else:
            records[name] = 'missing'
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'sha256': digest, 'paths': list(MEASUREMENT_PATHS), 'files': records}


def _bound_json(path):
    path = Path(path)
    raw = path.read_bytes()
    return json.loads(raw), dict(path=str(path.resolve()), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


def _verify_tar(path, expected, guest_manifest=None):
    try:
        with tarfile.open(path, 'r:') as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            required = set(expected) | ({'guest_manifest.json'} if guest_manifest is not None else set())
            _check(len(names) == len(set(names)) and set(names) == required,
                   'archive member set differs: ' + str(path))
            for member in members:
                _check(member.isfile(), 'non-file archive member: ' + member.name)
                raw = archive.extractfile(member).read()
                if member.name == 'guest_manifest.json' and guest_manifest is not None:
                    _check(json.loads(raw) == guest_manifest, 'guest archive manifest differs')
                else:
                    record = expected[member.name]
                    _check(len(raw) == record.get('size', record.get('bytes')) and
                           hashlib.sha256(raw).hexdigest() == record['sha256'],
                           'archive member bytes changed: ' + member.name)
    except (tarfile.TarError, OSError, KeyError) as error:
        raise ValueError('Figure 9 reuse refused: invalid archive ' + str(path)) from error


def verify_archives(config, root, repo, actions_path):
    files = config['sources']['files']
    record = config['sources'].get('archive', {})
    archive = root / 'guest.tar'
    _check(record.get('path') == str(archive) and record.get('size') == archive.stat().st_size
           and record.get('sha256') == _sha(archive), 'guest archive SHA/size changed')
    _verify_tar(archive, files, guest_manifest=files)
    expected = {}
    for record in config['extra_sources']:
        path = Path(record['path'])
        if path.resolve() == actions_path:
            name = 'actions.json'
        elif path.parent == root and path.name in ('lower.tar', 'experiment.json'):
            name = path.name
        elif path.parent == Path(repo) / 'ae/vendor/d-overlayfs/agentfs':
            name = 'agentfs/' + path.name
        else:
            _check(path.is_relative_to(Path(repo) / 'ae'), 'unexpected extra source location')
            name = path.name
        _check(name not in expected, 'duplicate extra archive destination: ' + name)
        expected[name] = record
    _check({'actions.json', 'lower.tar', 'experiment.json'} <= set(expected), 'incomplete extra archive inputs')
    _verify_tar(root / 'extra.tar', expected)


def verify_imported_job(job, suite):
    """Revalidate copied provenance on ordinary same-source --resume."""
    descriptor = suite.get('reuse_manifest') or {}
    path = Path(descriptor.get('path', ''))
    imported, actual = _bound_json(path)
    _check(actual == descriptor and imported.get('status') == 'copied-and-verified',
           'reuse manifest changed or is incomplete')
    receipts = [item for item in imported.get('jobs', []) if item.get('job') == job['key']]
    _check(len(receipts) == 1, 'reuse job receipt missing/duplicate')
    receipt = receipts[0]
    root = Path(_arg(job['command'], '--out'))
    origin_path = root / 'reuse-origin.json'
    _check(job.get('reuse_origin') == str(origin_path), 'reuse origin location differs')
    expected_origin = dict(kind=imported['kind'], original_root=str(Path(receipt['run']['path']).parent),
        original_review=imported['original_review'], original_suite=imported['original_suite'],
        planner_release=imported['planner_release'],
        measurement_fingerprint_sha256=imported['measurement_fingerprint']['sha256'], **receipt)
    _check(json.loads(origin_path.read_text()) == expected_origin, 'reuse origin receipt changed')
    records = _tree_records(root)
    records.pop('reuse-origin.json', None)
    _check(records == receipt['files'], 'imported job files changed')
    config, current = _bound_json(root / 'run.json')
    _check(current['sha256'] == receipt['run']['sha256'] and current['bytes'] == receipt['run']['bytes']
           and config.get('release') == receipt['release'] == job.get('measurement_release')
           and job.get('original_run') == receipt['run'], 'imported measurement source changed')
    return receipt


def validate_job(prior, job, old_suite, source, *, repo, verify_images, claimed):
    """Validate a candidate without writing or changing any old/new output."""
    from ae.repro.analysis import Evidence, FreshRun, source_identity
    _check(prior.get('experiment') == job.get('experiment') == 'figure-09', 'non-Figure 9 job')
    _check(prior.get('status') == 'ok', 'job did not succeed')
    _check(prior.get('run_purpose') == 'full-cohort' and job.get('run_purpose') == 'full-trace', 'partial action scope')
    _check(_normalized_command(prior['command']) == _normalized_command(job['command']),
           'measurement command/input/arm changed for ' + job['key'])
    _check(prior.get('inputs') == job.get('inputs'), 'planned input changed')
    root = _plain_path(_arg(prior['command'], '--out'), source)
    _check(root == source / 'runs' / 'figure-09' / job['key'], 'unexpected original job location')
    _check(not (root / 'reuse-origin.json').exists(), 'chained imports require an explicit original source')
    initial_files = _tree_records(root)
    process_path = _plain_path(prior['process_manifest'], source)
    process, process_record = _bound_json(process_path)
    _check(process.get('status') == 'ok' and process.get('returncode') == 0 and process.get('finished_at'),
           'outer job process lacks successful terminal state')
    _check(process.get('command') == prior['command'], 'process command differs from suite')
    _check(prior.get('staging_cleanup', {}).get('status') in ('ok', 'not-applicable'), 'cleanup did not succeed')
    run = FreshRun(Evidence(root, 'fresh'), root / 'run.json', claimed)
    config = run.config
    _check(config.get('experiment') == 'figure-09' and config.get('run_purpose') == 'full-cohort',
           'manifest experiment/scope differs')
    key, arm = _arg(job['command'], '--input-key'), _arg(job['command'], '--arm')
    _check(config.get('input_key') == key and config.get('arm') == arm, 'manifest input/arm mismatch')
    actions_path = Path(_arg(job['command'], '--actions')).resolve(strict=True)
    actions = json.loads(actions_path.read_text())
    _check(config.get('expected_edits') == job.get('expected_edits') == len(actions['edits']), 'action count changed')
    expected_indices = [edit['edit_idx'] for edit in actions['edits']]
    _check(len(set(expected_indices)) == len(expected_indices), 'duplicate input action indices')
    rows = run.jsonl(f'measurements/{key}_{arm}.jsonl')
    _check(len(rows) == len(actions['edits']), 'incomplete measured actions')
    for row in rows:
        _check(not row.get('error') and row.get('instance') == key and row.get('fs_arm') == arm,
               'failed/mismatched measured action')
        if row.get('measurement_status') == 'excluded-input':
            _check(row.get('exclusion_reason') == 'historical-diff-out-of-bounds' and
                   row.get('applied_ok') is False and row.get('copyup_bytes') is None and
                   row.get('phys_bytes') is None, 'invalid recorded input exclusion')
            number(row['file_size_bytes'], 'file_size_bytes')
        else:
            _check(row.get('applied_ok') is True, 'edit action did not apply')
            for field in ('file_size_bytes', 'copyup_bytes', 'phys_bytes'):
                number(row[field], field)
    _check([row['edit_idx'] for row in rows] == sorted(expected_indices), 'recorded action coverage/order differs')
    edits = {edit['edit_idx']: edit for edit in actions['edits']}
    _check(all(row.get('file_path') == edits[row['edit_idx']]['file_path'] for row in rows),
           'recorded edit target changed')
    audit = run.json('measurements/input-audit.json')
    _check(audit.get('requested_edits') == len(expected_indices) and
           audit.get('eligible_edits', -1) + len(audit.get('excluded', [])) == len(expected_indices),
           'input audit is incomplete')
    repository = config.get('repository_source', {})
    _check(repository.get('commit') == actions['base_commit'] and
           repository.get('files') == sorted({edit['file_path'] for edit in actions['edits']}),
           'base commit or lower file set changed')
    _check(config.get('filesystem_geometry', {}).get('logical_bytes') == 4 * 1024**3,
           'filesystem geometry changed')
    storage = run.json('measurements/storage.json')
    host_storage = run.json('host-storage.json')
    _check(storage.get('backing_fstype') == host_storage.get('fstype') == 'tmpfs' and
           'noswap' in storage.get('mount_options', []) and
           'noswap' in host_storage.get('mount', {}).get('options', '').split(',') and
           storage.get('filesystem_arm') == arm and storage.get('logical_bytes') == 4 * 1024**3,
           'memory storage/arm/geometry evidence differs')
    _check(config.get('host', {}).get('cpu_affinity') == old_suite.get('host', {}).get('cpu_affinity'),
           'worker affinity differs from measured suite')
    extras = config.get('extra_sources', [])
    _check(any(Path(r['path']).resolve() == actions_path for r in extras), 'input lacks original content hash')
    for record in extras:
        path = Path(record['path'])
        if path.is_relative_to(root) or path.resolve() == actions_path:
            _check(path.is_file() and path.stat().st_size == record['bytes'] and _sha(path) == record['sha256'],
                   'input/guest dependency changed: ' + str(path))
    files = config.get('sources', {}).get('files', {})
    _check(bool(files), 'guest payload fingerprint missing')
    # Check the original payload in guest.tar/extra.tar, not today's checkout.
    verify_archives(config, root, repo, actions_path)
    _check(set(config.get('images', {})) == {'kernel', 'base_xfs', 'data_xfs'}, 'image fingerprint incomplete')
    verify_images(config)
    inner = json.loads((root / 'process/process.json').read_text())
    _check(inner.get('status') == 'ok' and inner.get('returncode') == 0 and inner.get('finished_at'),
           'VM process lacks successful terminal state')
    _check(_tree_records(root) == initial_files and file_record(process_path) == process_record,
           'job evidence changed during validation')
    run_record = dict(path=str(root / 'run.json'), **initial_files['run.json'])
    return root, dict(job=job['key'], source_identity=source_identity(config),
                     release=config['release'], input=file_record(actions_path),
                     process=process_record, run=run_record,
                     expected_edits=len(expected_indices), filesystem_arm=arm,
                     files=initial_files)


def prepare_reuse(plan, source, destination, *, repo, verify_images, check_active, validate_only=False):
    """Validate *all* candidates, then copy complete original bytes without links.

    Caller holds the main lane/results gate. The source is never moved/edited.
    Imported manifests keep their original release; only the planner is new.
    """
    source, destination = Path(source).absolute(), Path(destination).absolute()
    _check(source.resolve(strict=True) == source, 'source contains a symlink')
    _check(not destination.is_relative_to(source) and not source.is_relative_to(destination),
           'source and destination must be disjoint')
    review_path, suite_path = source / 'review.json', source / 'runs/figure-09/suite.json'
    review, review_record = _bound_json(review_path)
    old, suite_record = _bound_json(suite_path)
    _check(review.get('status') in ('ok', 'failed', 'interrupted') and review.get('finished_at'),
           'source review is not terminal')
    _check(old.get('status') in ('ok', 'failed', 'interrupted'), 'source stage is active')
    _check(plan.get('experiments') == ['figure-09'], 'reuse only supports Figure 9')
    _check(old.get('effective_config_sha256') == plan.get('effective_config_sha256'), 'effective config changed')
    _check(old.get('measurement_identity') == plan.get('measurement_identity'), 'NUMA/CPU/frequency/resource policy changed')
    _check(old.get('run_purpose') == 'full-cohort' and plan.get('run_purpose') in ('full-cohort', 'full-trace'), 'partial run purpose')
    _check(not check_active(source), 'source has active file/mapping/mount references')
    fingerprint = measurement_fingerprint(repo, old['release']['source_commit'])
    original = {job['key']: job for job in old['jobs']}
    _check(len(original) == len(old['jobs']), 'duplicate source job keys')
    _check(len({job['key'] for job in plan['jobs']}) == len(plan['jobs']), 'duplicate new job keys')
    selected = {job['key'] for job in plan['jobs']}
    candidates, claimed = [], set()
    for job in plan['jobs']:
        prior = original.get(job['key'])
        if prior and prior.get('status') == 'ok':
            root, receipt = validate_job(prior, job, old, source, repo=repo,
                                        verify_images=verify_images, claimed=claimed)
            candidates.append((job, root, receipt))
    _check(bool(candidates), 'no complete matching jobs to reuse')
    total = sum(row['bytes'] for _, _, receipt in candidates for row in receipt['files'].values())
    ancestor = destination
    while not ancestor.exists():
        ancestor = ancestor.parent
    _check(shutil.disk_usage(ancestor).free >= total + MIN_FREE_BYTES, 'copy would violate 10 GiB free-space reserve')
    origin = dict(schema_version=1, kind='verified-figure09-job-reuse', status='validated',
                  original_review=review_record, original_suite=suite_record,
                  original_release=old['release'], planner_release=plan['release'],
                  measurement_fingerprint=fingerprint, measurement_identity=plan['measurement_identity'],
                  copied_bytes=total, reused_jobs=len(candidates), selected_jobs=len(plan['jobs']),
                  excluded_completed_jobs=sorted(key for key, job in original.items()
                                                 if job.get('status') == 'ok' and key not in selected),
                  analysis_policy='Original source identities remain separate statistical populations.',
                  jobs=[receipt for _, _, receipt in candidates])
    # A late source mutation or newly active reference must fail before copying.
    _check(file_record(review_path) == origin['original_review'] and file_record(suite_path) == origin['original_suite'],
           'source control files changed during validation')
    _check(not check_active(source), 'source acquired active references during validation')
    if validate_only:
        return origin
    _check(not destination.exists(), 'destination stage already exists')
    destination.mkdir(parents=True)
    for job, root, receipt in candidates:
        target = destination / job['key']
        _check(_tree_records(root) == receipt['files'], 'source job changed before copy')
        shutil.copytree(root, target, copy_function=shutil.copy2)
        _check(_tree_records(root) == receipt['files'] == _tree_records(target), 'source/copy content mismatch')
        write_json(target / 'reuse-origin.json', dict(kind=origin['kind'], original_root=str(root),
                   original_review=origin['original_review'], original_suite=origin['original_suite'],
                   planner_release=origin['planner_release'], measurement_fingerprint_sha256=fingerprint['sha256'], **receipt))
        job.update(status='ok', reused_verified=True, execution='copied-completed-measurement',
                   measurement_release=receipt['release'], original_run=receipt['run'],
                   process_manifest=receipt['process']['path'], reuse_origin=str(target / 'reuse-origin.json'))
    origin['status'] = 'copied-and-verified'
    write_json(destination / 'reuse-manifest.json', origin)
    plan.update(import_verified=True, measurement_sources=[old['release'], plan['release']],
                reuse_manifest=file_record(destination / 'reuse-manifest.json'))
    return origin
