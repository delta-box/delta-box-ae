"""Verified references to completed original-cohort E2B inputs.

Measurements stay at their original paths and keep their original release.
Only incomplete inputs are executed in the new owned L1. No old result, failed
prefix, driver command, or source identity is rewritten or copied as new data.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess

from ae.repro.common import file_record, write_json
from ae.repro.figure09_reuse import _arg, _bound_json, _plain_path
from ae.repro.cube_reuse import source_commit_identity
from ae.scripts.e2b_paper_profile import COHORT

EXPERIMENT = 'table-02-e2b'
KIND = 'verified-e2b-paper-input-reference-v1'
EXECUTION = 'referenced-completed-measurement'


def require(condition, message):
    if not condition:
        raise ValueError('E2B reuse refused: ' + message)


def validate_plan(plan):
    require(plan.get('experiments') == [EXPERIMENT] and plan.get('workers', 1) == 1
            and plan.get('measurement_identity', {}).get('e2b_profile') == 'paper-nested',
            'complete serial paper-nested plan required')
    require([j.get('key') for j in plan.get('jobs', [])] == [EXPERIMENT+'__'+r[0] for r in COHORT]
            and all(j.get('experiment') == EXPERIMENT and j.get('run_purpose') == 'full-trace'
                    for j in plan['jobs']), 'original eight ordered complete inputs required')


def bound(root, descriptor, *, within=None):
    path = Path(descriptor['path'])
    if not path.is_absolute():
        path = root / path
    if within is not None:
        path = _plain_path(path, within)
    actual = file_record(path)
    require(all(actual[k] == descriptor[k] for k in ('sha256', 'bytes')), 'artifact changed: '+str(path))
    return path, actual


def frozen_controls(proof):
    from ae.scripts import e2b_paper_profile as profile
    expected_paths = {'manifest': profile.MANIFEST,
        'contract': profile.INPUT_ROOT/'e2b-paper-185-input-action-contract.json'}
    records = []
    for name, path in expected_paths.items():
        descriptor = proof[name]
        require(descriptor.get('path') == str(path), 'frozen '+name+' path changed')
        raw = profile._root_read(path)
        digest = hashlib.sha256(raw).hexdigest()
        require(digest == descriptor.get('sha256') and
                ('bytes' not in descriptor or descriptor['bytes'] == len(raw)), 'frozen '+name+' bytes changed')
        if name == 'contract':
            require(digest == profile.CONTRACT_SHA256, 'original contract identity changed')
        records.append(dict(path=str(path), sha256=digest, bytes=len(raw)))
    return records


def validate_lifecycle_identity(state, life):
    identity = state.get('identity')
    require(isinstance(identity, dict) and identity and life.get('ownership') == identity
            and life.get('resources', {}).get('cgroup') == identity.get('cgroup'),
            'lifecycle ownership/resources belong to a different L1')


def validate_runtime_identity(before, after, deployment, deployment_sha, state, fresh):
    files = deployment.get('files')
    require(deployment.get('schema_version') == 1 and deployment.get('kind') == 'e2b-paper-runtime-deployment-v1'
            and isinstance(files, list) and files
            and all(isinstance(r, dict) and isinstance(r.get('path'), str) and r['path']
                    and re.fullmatch('[0-9a-f]{64}', r.get('sha256', '')) for r in files)
            and len({r['path'] for r in files}) == len(files), 'runtime deployment is empty/incomplete')
    require(deployment_sha == state.get('deployment_sha256') == fresh.get('runtime_manifest_sha'),
            'runtime deployment/fresh base/state identities differ')
    require(before.get('status') == after.get('status') == 'verified'
            and before.get('files') == after.get('files') == files,
            'runtime pre/post evidence does not cover the bound deployment')


def load_source(source, plan, repo, check_active=None, *, analysis=False):
    source = Path(source).absolute()
    require(source.resolve(strict=True) == source, 'source must be a plain canonical result path')
    validate_plan(plan)
    review, rr = _bound_json(source/'review.json')
    suite, sr = _bound_json(source/'runs'/EXPERIMENT/'suite.json')
    validate_plan(suite)
    review_ready = (review.get('status') in ('ok', 'failed', 'interrupted') and review.get('finished_at')) or (analysis and review.get('status') == 'running')
    require(review_ready and suite.get('status') in ('ok', 'failed', 'interrupted'), 'source is not terminal')
    require(review.get('experiments') == [EXPERIMENT] and review.get('e2b_profile') == 'paper-nested',
            'source profile differs')
    require(suite.get('e2b_paper_inputs') == plan.get('e2b_paper_inputs'), 'frozen original inputs changed')
    frozen = frozen_controls(plan['e2b_paper_inputs'])
    if check_active is not None:
        require(not check_active(source), 'source has active file, mapping or mount references')
    state = None
    controls = [sr, *frozen] if analysis else [rr, sr, *frozen]
    local_successes = [j for j in suite['jobs'] if j.get('status') == 'ok' and not j.get('reused_verified')]
    if local_successes:
        state, state_record = _bound_json(source/'runs/e2b-paper-l1/state.json')
        require(state.get('status') in ('completed', 'failed') and state.get('finished_unix'), 'L1 suite unfinished')
        life_path = _plain_path(state['lifecycle'], Path(repo))
        life, life_record = _bound_json(life_path)
        require(life.get('status') in ('completed', 'failed') and life.get('finished_unix')
                and life.get('qemu_returncode') == 0 and not life.get('cleanup_error'), 'owned L1 not cleanly reaped')
        validate_lifecycle_identity(state, life)
        cgroup = state.get('identity', {}).get('cgroup', '')
        require(re.fullmatch(r'/system.slice/[A-Za-z0-9_.@:-]+\.service', cgroup), 'missing owning unit identity')
        if analysis:
            identity = state['identity']
            require(type(identity.get('pid')) is int and identity['pid'] > 0
                    and type(identity.get('starttime')) is int, 'missing QEMU process identity')
            try:
                live_stat = (Path('/proc')/str(identity['pid'])/'stat').read_text()
            except FileNotFoundError:
                live_stat = None
            if live_stat is not None:
                starttime = int(live_stat.rsplit(')', 1)[1].split()[19])
                require(starttime != identity['starttime'], 'QEMU still alive during analysis')
        else:
            raw = subprocess.check_output(['systemctl', 'show', cgroup.rsplit('/', 1)[-1],
                '--property=MainPID,ActiveState,ControlGroup'], text=True)
            owner = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
            require(owner.get('MainPID') == '0' and owner.get('ActiveState') in ('inactive', 'failed'), 'old owner still active')
            cg = Path('/sys/fs/cgroup')/cgroup.lstrip('/')
            require(not cg.exists() or not any(p.read_text().split() for p in cg.rglob('cgroup.procs')),
                    'old cgroup still populated')

        controls.extend([state_record, life_record])
    identity = source_commit_identity(repo, suite['release'])
    return dict(root=source, review=review, suite=suite, state=state,
                controls=controls, source_identity=identity)


def validate_source_inventory(sources, root):
    require(isinstance(sources, list) and sources
            and all(isinstance(r, dict) and isinstance(r.get('path'), str) for r in sources),
            'source inventory missing')
    paths = {r['path'] for r in sources}
    require(len(paths) == len(sources), 'duplicate source inventory paths')
    required = {str(root/'driver'/name) for name in (
        'e2b_paper_nested_driver.py', 'e2b_slim_finalbench_pilot.py',
        'e2b_slim_action_runner.py', 'e2b_paper_action.py')}
    require(required <= paths, 'required actual staged driver source missing')


def validate_measured_job(prior, job, context, plan):
    from ae.runners.baseline import validate_paper_e2b
    from ae.scripts.e2b_paper_suite import completed_build_ids, validate_post_closure
    source = context['root']; instance = _arg(job['command'], '--instance')
    expected = next(r for r in COHORT if r[0] == instance)
    require(prior.get('key') == job['key'] and prior.get('status') == 'ok'
            and not prior.get('reused_verified'), 'candidate is not an original successful measurement')
    root = _plain_path(_arg(prior['command'], '--out'), source)
    require(root == source/'runs'/EXPERIMENT/job['key'], 'unexpected original input path')
    run, run_record = _bound_json(root/'run.json')
    require(run.get('status') == 'ok' and run.get('instance') == instance and run.get('backend') == 'e2b'
            and run.get('experiment') == EXPERIMENT and run.get('run_purpose') == 'full-trace', 'run identity/status differs')
    require(run.get('message_policy') == 'strict' and run.get('e2b_worker_mode') == 'cold'
            and run.get('counts') == {'checkpoints': expected[5], 'restores': expected[5]}, 'partial or different worker contract')
    require(run.get('paper_input_contract') == plan['e2b_paper_inputs'], 'run frozen contract differs')
    checks = {r['path']: r for r in [*context['controls'], run_record]}
    for process_path, command in [(prior['process_manifest'], prior['command']), (root/'process/process.json', run['command'])]:
        pp = _plain_path(process_path, source); process, pr = _bound_json(pp)
        require(process.get('status') == 'ok' and process.get('returncode') == 0
                and process.get('finished_at') and process.get('command') == command, 'process did not complete successfully')
        checks[pr['path']] = pr
    selected = next(r for r in plan['e2b_paper_inputs']['inputs'] if r['instance'] == instance)
    require(_arg(prior['command'], '--trace') == selected['trajectory']['path']
            and _arg(prior['command'], '--repository-commit') == selected['repository_commit'], 'input/base changed')
    trace, rec = bound(root, selected['trajectory']); checks[rec['path']] = rec
    validate_source_inventory(run.get('sources'), root)
    for item in run['sources']:
        _, rec = bound(root, item); checks[rec['path']] = rec
    artifacts = run.get('artifacts', [])
    require(artifacts and len({r['path'] for r in artifacts}) == len(artifacts), 'missing/duplicate raw artifacts')
    for item in artifacts:
        _, rec = bound(root, item, within=root); checks[rec['path']] = rec
    pilot, rec = bound(root, run['result'], within=root); checks[rec['path']] = rec
    require(run['result'] in artifacts, 'pilot is not a bound raw artifact')
    validate_paper_e2b(pilot, trace, instance, plan['e2b_paper_inputs'], run['e2b_environment'], root)
    post = prior.get('paper_post_validation', {})
    require(post.get('status') == 'verified' and post.get('base_unchanged') is True, 'post-input snapshot validation missing')
    l1 = source/'runs/e2b-paper-l1'/instance
    proofs = []
    for name in ('base-before.json', 'base-after.json', 'all-builds-after.json'):
        value, rec = _bound_json(l1/name); proofs.append(value); checks[rec['path']] = rec
    require(file_record(l1/'all-builds-after.json')['sha256'] == post['sha256']
            and post['all_builds_manifest'] == str(l1/'all-builds-after.json'), 'snapshot proof binding changed')
    base = run['e2b_environment']['from_build']
    created = completed_build_ids(root, set(proofs[0]['builds']), expected[5], base)
    validate_post_closure(*proofs, proofs[0]['storage'], base, created)
    fresh_path, rec = bound(root, run['e2b_environment']['fresh_base_manifest'], within=source)
    checks[rec['path']] = rec
    fresh = json.loads(fresh_path.read_text())
    require(fresh == run['e2b_environment']['fresh_base_proof'] and fresh['snapshot_closure'] == proofs[0], 'fresh-base evidence differs')
    before, br = _bound_json(source/'runs/e2b-paper-l1/runtime-before.json')
    after, ar = _bound_json(l1/'runtime-after.json')
    deployment, dr = _bound_json(source/'runs/e2b-paper-l1/runtime-deployment.json')
    validate_runtime_identity(before, after, deployment, dr['sha256'], context['state'], fresh)
    checks[br['path']] = br; checks[ar['path']] = ar; checks[dr['path']] = dr
    for rec in checks.values():
        require(file_record(Path(rec['path'])) == rec, 'evidence changed during validation')
    return dict(job=job['key'], instance=instance, original_source=str(source), run=run_record,
                process=file_record(Path(prior['process_manifest'])), release=run['release'],
                source_identity=context['source_identity'], counts=run['counts'],
                expansions=expected[4], checks=list(checks.values()))


def verify_referenced_job(job, plan):
    require(job.get('execution') == EXECUTION and job.get('reused_verified') is True, 'unbound reuse marker')
    descriptor = plan.get('reuse_manifest') or {}
    origin, actual = _bound_json(descriptor.get('path', ''))
    require(actual == descriptor and origin.get('kind') == KIND and origin.get('status') == 'referenced-and-verified',
            'reference manifest changed/incomplete')
    rows = [r for r in origin['jobs'] if r['job'] == job['key']]
    require(len(rows) == 1, 'missing/duplicate source receipt')
    receipt = rows[0]
    require(job.get('original_run') == receipt['run'] and job.get('measurement_release') == receipt['release'],
            'reference source identity changed')
    repo = Path(__file__).resolve().parents[2]
    context = load_source(receipt['original_source'], plan, repo)
    prior = next(j for j in context['suite']['jobs'] if j['key'] == job['key'])
    fresh = validate_measured_job(prior, job, context, plan)
    require(fresh == receipt, 'original evidence no longer matches verified receipt')
    return receipt


def validate_destination(source, destination):
    require(destination.resolve(strict=False) == destination, 'destination contains a symlink or path alias')
    require(not destination.is_relative_to(source) and not source.is_relative_to(destination),
            'source/destination overlap')


def prepare_reuse(plan, source, destination, *, repo, verify_images=None, check_active, validate_only=False):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    validate_destination(source, destination)
    context = load_source(source, plan, repo, check_active)
    old = {j['key']: j for j in context['suite']['jobs']}; candidates = []
    for job in plan['jobs']:
        prior = old[job['key']]
        if prior.get('status') != 'ok':
            continue
        receipt = (verify_referenced_job(prior, context['suite']) if prior.get('reused_verified')
                   else validate_measured_job(prior, job, context, plan))
        candidates.append((job, receipt))
    require(candidates, 'no complete original inputs to reuse')
    require(not check_active(source), 'source acquired active references')
    origin = dict(schema_version=1, kind=KIND, status='validated',
                  original_review=context['controls'][0], original_suite=context['controls'][1],
                  original_release=context['suite']['release'], planner_release=plan['release'],
                  reused_jobs=len(candidates), selected_jobs=len(COHORT),
                  analysis_policy='Aggregate only through explicit per-input original source references; no data copied or relabelled.',
                  jobs=[receipt for _, receipt in candidates])
    if validate_only:
        return origin
    validate_destination(source, destination)
    require(not destination.exists(), 'destination stage already exists')
    destination.mkdir(parents=True)
    for job, receipt in candidates:
        job.update(status='ok', reused_verified=True, execution=EXECUTION,
                   measurement_release=receipt['release'], original_run=receipt['run'],
                   process_manifest=receipt['process']['path'])
    origin['status'] = 'referenced-and-verified'
    path = destination/'reuse-manifest.json'; write_json(path, origin)
    releases = {json.dumps(r['release'], sort_keys=True): r['release'] for _, r in candidates}
    releases[json.dumps(plan['release'], sort_keys=True)] = plan['release']
    plan.update(import_verified=True, measurement_sources=list(releases.values()), reuse_manifest=file_record(path))
    return origin
