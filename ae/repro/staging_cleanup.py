"""Release private, reconstructable baseline staging after the producer exits.

Measured artifacts and the private adapted Moatless sources stay in the run.
This module never runs inside a checkpoint, restore or replay timing window.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from .common import file_record, write_json


TARGETS = ('payload/repos', 'nltk_data')


def private_directory(root: Path, relative: str) -> Path:
    target = root
    for part in Path(relative).parts:
        target /= part
        info = target.lstat()
        if not stat.S_ISDIR(info.st_mode) or target.is_symlink():
            raise ValueError(f'Cleanup requires a private directory, not a symlink: {target}')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Cleanup target escapes run: {target}')
    return target


def footprint(path: Path) -> dict:
    """Count allocated blocks without following links or counting hardlinks twice."""
    seen, logical, allocated, files = set(), 0, 0, 0
    device = path.stat().st_dev
    for directory, dirs, names in os.walk(path, followlinks=False):
        for entry in [Path(directory), *(Path(directory) / name for name in names + dirs)]:
            info = entry.lstat()
            if info.st_dev != device or (stat.S_ISDIR(info.st_mode) and entry != path and os.path.ismount(entry)):
                raise ValueError(f'Cleanup refuses a mounted directory: {entry}')
            identity = info.st_dev, info.st_ino
            if identity in seen:
                continue
            seen.add(identity)
            logical += info.st_size
            allocated += getattr(info, 'st_blocks', 0) * 512
            files += stat.S_ISREG(info.st_mode)
    return dict(logical_bytes=logical, allocated_bytes=allocated, regular_files=files)


def verify_artifacts(root: Path):
    from .analysis import Evidence, FreshRun
    return FreshRun(Evidence(root, 'fresh'), root / 'run.json', set())


def _e2b_child(path, storage, trust):
    """Inspect exactly one claimed child, never follow links or scan siblings."""
    from ae.runners.e2b_environment import SNAPSHOT_FILES
    from ae.scripts.hosted_launcher import trusted_path
    trusted_path(path.parent, directory=True, trust=trust)
    if not path.exists() and not path.is_symlink():
        return None
    trusted_path(path, directory=True, trust=trust)
    if os.path.ismount(path) or path.stat().st_dev != storage.stat().st_dev:
        raise ValueError(f'Cleanup refuses a mounted E2B child: {path}')
    if {entry.name for entry in path.iterdir()} != set(SNAPSHOT_FILES):
        raise ValueError(f'E2B child has unexpected or incomplete snapshot files: {path}')
    identities = {}
    for entry in [path, *(path / name for name in SNAPSHOT_FILES)]:
        trusted_path(entry, directory=entry == path, trust=trust)
        info = entry.lstat()
        if entry != path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError(f'E2B child file is not an independent regular file: {entry}')
        if info.st_dev != storage.stat().st_dev:
            raise ValueError(f'E2B child crosses a filesystem boundary: {entry}')
        identities[entry.name] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return dict(identity=identities, **footprint(path))


def validate_e2b_storage(config):
    from ae.scripts import hosted_launcher as hosted
    policy = hosted.load_policy()
    trust = hosted.runtime_trust(policy)
    fixed = hosted.read_root_json(policy['config']).get('e2b', {})
    chosen = config.get('e2b', {})
    for key in ('execution', 'storage', 'from_build', 'parent_manifest'):
        if not fixed.get(key) or chosen.get(key) != fixed[key]:
            raise ValueError(f'E2B fixed configuration differs: {key}')
    storage = hosted.trusted_path(fixed['storage'], directory=True, trust=trust)
    work = policy['runtime_root'] / 'ae/work'
    if storage == work or not storage.is_relative_to(work):
        raise ValueError('E2B cleanup storage must stay inside the fixed runtime ae/work')
    parent = hosted.trusted_path(fixed['parent_manifest'], trust=trust, root_leaf=True)
    if not parent.is_relative_to(storage):
        raise ValueError('E2B parent manifest must stay inside the fixed storage')
    return storage, parent


def _e2b_cleanup_plan(root, producer):
    """Bind deletion authority to root policy and this producer's hashed result."""
    from ae.runners.e2b_environment import _parent_dependencies, verify_snapshot_inputs
    from ae.scripts import hosted_launcher as hosted
    policy = hosted.load_policy()
    trust = hosted.runtime_trust(policy)
    hosted.trusted_path(root, directory=True, trust=trust)
    hosted.trusted_path(root / 'run.json', trust=trust, root_leaf=True)
    output = hosted.trusted_path(policy['output_root'], directory=True, trust=trust, root_leaf=True)
    if root == output or not root.is_relative_to(output):
        raise ValueError('E2B cleanup producer is outside the fixed output root')
    fixed = hosted.read_root_json(policy['config']).get('e2b', {})
    recorded = producer.get('config', {}).get('e2b', {})
    evidence = producer.get('e2b_environment', {})
    if (producer.get('backend') != 'e2b' or producer.get('experiment') != 'table-02-e2b'
            or any(value.get('execution') != 'local' for value in (fixed, recorded, evidence))):
        raise ValueError('E2B cleanup requires matching local E2B execution identities')
    for key in ('storage', 'from_build', 'parent_manifest'):
        if not fixed.get(key) or recorded.get(key) != fixed[key]:
            raise ValueError(f'E2B cleanup fixed configuration differs: {key}')
    storage = hosted.trusted_path(fixed['storage'], directory=True, trust=trust)
    work = policy['runtime_root'] / 'ae/work'
    if storage == work or not storage.is_relative_to(work):
        raise ValueError('E2B cleanup storage must stay inside the fixed runtime ae/work')
    if evidence.get('storage') != str(storage) or evidence.get('snapshot_inputs_unchanged') is not True:
        raise ValueError('E2B cleanup lacks verified matching storage inputs')
    parent = hosted.trusted_path(fixed['parent_manifest'], trust=trust, root_leaf=True)
    if not parent.is_relative_to(storage):
        raise ValueError('E2B parent manifest must stay inside the fixed storage')
    parent_identity = file_record(parent)
    if evidence.get('parent_manifest') != parent_identity:
        raise ValueError('E2B parent manifest identity differs from the measured input')
    parents = json.loads(parent.read_text())
    dependencies = _parent_dependencies(parents, storage, fixed['from_build'])
    if evidence.get('snapshot_dependencies') != dependencies:
        raise ValueError('E2B cleanup parent closure differs from the measured inputs')
    for item in dependencies:
        hosted.trusted_path(item['path'], trust=trust)
    verify_snapshot_inputs(evidence)

    result = producer.get('result', {})
    relative = Path(result.get('path', ''))
    if not relative.parts or relative.is_absolute() or '..' in relative.parts or relative.name != 'pilot_result.json':
        raise ValueError('E2B cleanup requires a local pilot result identity')
    pilot = hosted.trusted_path(root / relative, trust=trust, root_leaf=True)
    matches = [item for item in producer.get('artifacts', []) if item.get('path') == str(relative)]
    actual = file_record(pilot)
    if (len(matches) != 1 or any(actual[key] != result.get(key) or actual[key] != matches[0].get(key)
                                  for key in ('bytes', 'sha256'))):
        raise ValueError('E2B pilot result is not SHA-bound to this producer')
    measured = json.loads(pilot.read_text())
    created = measured.get('create', {})
    setup = measured.get('root_setup', {})
    if (measured.get('ok') is not True or measured.get('instance') != producer.get('instance')
            or created.get('ok') is not True or created.get('build_id') != fixed['from_build'] or created.get('reused') is not True
            or setup.get('ok') is not True or setup.get('reused', False) is not False):
        raise ValueError('E2B cleanup cannot confirm ownership of a reused or unverified root build')
    iterations = measured.get('iterations')
    if not isinstance(iterations, list) or any(not isinstance(row, dict) or row.get('ok') is not True
                                              or not isinstance(row.get('e2b_steps'), list)
                                              for row in iterations):
        raise ValueError('E2B cleanup has incomplete action build records')
    steps = [step for row in iterations for step in row['e2b_steps']]
    if type(measured.get('n_e2b_steps')) is not int or measured['n_e2b_steps'] != len(steps):
        raise ValueError('E2B cleanup action count differs from the measured result')
    protected = set(parents['headers'])
    owned = []
    for step in [setup, *steps]:
        build = step.get('to_build') if isinstance(step, dict) else None
        if not isinstance(build, str):
            raise ValueError('E2B cleanup lacks a host-generated to_build UUID')
        try:
            identity = uuid.UUID(build)
        except ValueError as error:
            raise ValueError('E2B cleanup requires canonical host-generated UUIDs') from error
        if str(identity) != build or identity.version != 4 or step.get('ok') is not True:
            raise ValueError('E2B cleanup requires successful canonical host-generated UUIDs')
        if build in protected:
            raise ValueError('E2B cleanup refuses a protected parent build')
        if build in owned:
            raise ValueError('E2B cleanup repeats a claimed child build')
        owned.append(build)
    rows = []
    for build in sorted(owned):
        target = storage / 'templates' / build
        inspected = _e2b_child(target, storage, trust)
        rows.append(dict(path=str(target), kind='e2b-child', build_id=build,
                         status='pending' if inspected else 'absent',
                         **(inspected or dict(identity=None, logical_bytes=0, allocated_bytes=0, regular_files=0))))
    bindings = [file_record(path) for path in (hosted.POLICY_PATH, policy['config'], parent, pilot)]
    context = dict(storage=storage, trust=trust, evidence=evidence, bindings=bindings,
                   protected_builds=sorted(protected), parent_manifest=parent_identity,
                   pilot_result=actual, rows=rows)
    return context


def _verify_e2b_context(context):
    from ae.runners.e2b_environment import verify_snapshot_inputs
    for expected in context['bindings']:
        if file_record(Path(expected['path'])) != expected:
            raise ValueError(f'E2B cleanup identity changed: {expected["path"]}')
    verify_snapshot_inputs(context['evidence'])


def cleanup_reconstructable_staging(root: Path) -> dict:
    root = Path(root)
    if root.is_symlink():
        raise ValueError('Cleanup refuses a symlink run directory')
    candidates = [name for name in TARGETS if (root / name).exists() or (root / name).is_symlink()]
    manifest = root / 'run.json'
    if not candidates and not manifest.exists() and not manifest.is_symlink():
        return dict(status='not-applicable', removed=[])
    if manifest.is_symlink():
        raise ValueError('Cleanup refuses a symlink producer manifest')
    config = json.loads(manifest.read_text())
    local_e2b = 'AE_HOSTED_CALLER_UID' in os.environ and (
        config.get('experiment') == 'table-02-e2b' or config.get('backend') == 'e2b') and (
        config.get('e2b_environment', {}).get('execution') == 'local'
        or config.get('config', {}).get('e2b', {}).get('execution') == 'local')
    if not candidates and not local_e2b:
        return dict(status='not-applicable', removed=[])
    if not getattr(shutil.rmtree, 'avoids_symlink_attacks', False):
        raise RuntimeError('Cleanup requires fd-based symlink-safe shutil.rmtree')
    if config.get('status') != 'ok' or config.get('analysis_mode') != 'fresh-measurement':
        raise ValueError('Cleanup requires a successful fresh producer manifest')
    report = root / 'staging-cleanup.json'
    if report.exists() or report.is_symlink():
        raise ValueError('Existing cleanup evidence must not be overwritten')
    targets = {name: private_directory(root, name) for name in candidates}
    # Validate the entire plan before removing even the ordinary local staging.
    verify_artifacts(root)
    e2b = _e2b_cleanup_plan(root, config) if local_e2b else None
    artifact_targets = list(targets.values()) + ([Path(row['path']) for row in e2b['rows']] if e2b else [])
    for artifact in config.get('artifacts', []):
        relative = Path(artifact['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe measured artifact path during cleanup')
        lexical, resolved = root / relative, (root / relative).resolve()
        for target in artifact_targets:
            if (lexical == target or lexical.is_relative_to(target) or
                    resolved == target.resolve() or resolved.is_relative_to(target.resolve())):
                raise ValueError(f'Cleanup target contains a measured artifact: {target}')
    identities = {}
    if 'payload/repos' in targets:
        metadata = root / 'repository.json'
        if metadata.is_symlink():
            raise ValueError('Cleanup refuses a symlink repository provenance record')
        repository = json.loads(metadata.read_text())
        instance = repository.get('instance')
        if instance != config.get('instance') or not isinstance(instance, str) or '/' in instance or '\\' in instance:
            raise ValueError('Cleanup repository identity differs from the producer')
        if not re.fullmatch(r'[0-9a-fA-F]{40}', str(repository.get('commit'))):
            raise ValueError('Cleanup repository requires its exact recorded commit')
        if sorted(path.name for path in targets['payload/repos'].iterdir()) != ['swe-bench_' + instance]:
            raise ValueError('Cleanup repository directory contains unexpected entries')
        private_directory(root, 'payload/repos/swe-bench_' + instance)
        source = Path(repository['source']).resolve()
        if not source.is_dir() or any(source.is_relative_to(target.resolve()) for target in targets.values()):
            raise ValueError('Cleanup repository has no separate reconstruction source')
        identities['payload/repos'] = dict(provenance=file_record(metadata), **repository)
    if 'nltk_data' in targets:
        dependency = config.get('local_dependencies', {}).get('nltk_data', {})
        source = Path(dependency.get('source', '')).resolve()
        staged = Path(dependency.get('staged', '')).resolve()
        records = dependency.get('files', [])
        if (not dependency.get('source') or not source.is_dir() or any(source.is_relative_to(target.resolve()) for target in targets.values())
                or staged != targets['nltk_data'].resolve() or not records):
            raise ValueError('Cleanup NLTK copy lacks reconstruction provenance')
        for item in records:
            if not re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256'))) or not Path(item['path']).resolve().is_relative_to(staged):
                raise ValueError('Cleanup NLTK identity is incomplete or outside its staging directory')
            original = source / Path(item['path']).resolve().relative_to(staged)
            identity = file_record(original)
            if any(identity[key] != item[key] for key in ('sha256', 'bytes')):
                raise ValueError(f'Cleanup NLTK reconstruction source differs from measured staging: {original}')
        identities['nltk_data'] = dict(source=str(source), manifest_field='local_dependencies.nltk_data',
                                       hashed_files=len(records))
    record = dict(schema_version=1, status='running', producer_manifest=file_record(manifest),
                  policy='successful producer exited; only reconstructable local staging and its verified local E2B child builds',
                  started_at=datetime.now(timezone.utc).isoformat(),
                  directories=[dict(path=name, status='pending', reconstruction=identities[name], **footprint(path))
                               for name, path in targets.items()])
    if e2b:
        record['e2b'] = dict(storage=str(e2b['storage']), protected_builds=e2b['protected_builds'],
                            parent_manifest=e2b['parent_manifest'], pilot_result=e2b['pilot_result'],
                            parent_input_count=len(e2b['evidence']['snapshot_dependencies']))
        record['directories'].extend(e2b['rows'])
    write_json(report, record)
    try:
        for row in record['directories']:
            # Recheck directory identity immediately before fd-based deletion.
            if row.get('kind') == 'e2b-child':
                target = Path(row['path'])
                current = _e2b_child(target, e2b['storage'], e2b['trust'])
                if current is None:
                    row['status'] = 'absent'
                    write_json(report, record)
                    continue
                if current['identity'] != row['identity']:
                    raise ValueError(f'E2B child changed after cleanup validation: {target}')
            else:
                target = private_directory(root, row['path'])
            shutil.rmtree(target)
            row['status'] = 'removed'
            write_json(report, record)
        verify_artifacts(root)
        if e2b:
            _verify_e2b_context(e2b)
            record['e2b']['parent_verification'] = 'unchanged after cleanup'
        if file_record(manifest) != record['producer_manifest']:
            raise ValueError('Producer manifest changed during staging cleanup')
        record.update(status='ok', artifact_verification='passed before and after cleanup')
    except BaseException as error:
        record.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        record['removed_allocated_bytes'] = sum(row['allocated_bytes'] for row in record['directories'] if row['status'] == 'removed')
        record['absent_allocated_bytes'] = sum(row['allocated_bytes'] for row in record['directories'] if row['status'] == 'absent')
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_json(report, record)
    return dict(status=record['status'], removed=[row['path'] for row in record['directories'] if row['status'] == 'removed'],
                absent=[row['path'] for row in record['directories'] if row['status'] == 'absent'],
                removed_allocated_bytes=record['removed_allocated_bytes'],
                absent_allocated_bytes=record['absent_allocated_bytes'], report=file_record(report))
