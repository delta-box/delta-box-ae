#!/usr/bin/env python3
"""Temporary, team-scoped official concurrency add-on for the native N64 run."""
from pathlib import Path
import json
import subprocess
import sys
import time

P = Path(__file__).resolve().parent
TEAM = '0b8a3ded-4489-4722-afd1-1d82e64ec2d5'
ADDON = 'c1034f24-8a87-4ba2-bf38-67e38b9647e2'
STATE = P / 'quota-before.json'

def query(sql):
    p = subprocess.run(['docker', 'exec', '-i', 'ae-e2b-postgres', 'psql',
                        '-U', 'postgres', '-d', 'postgres', '-At', '-v', 'ON_ERROR_STOP=1'],
                       input=sql, text=True, capture_output=True, check=True)
    return p.stdout.strip()

def limits():
    return int(query(f"SELECT concurrent_sandboxes FROM public.team_limits WHERE id='{TEAM}';"))

def invalidate():
    hashes = query(f"SELECT api_key_hash FROM public.team_api_keys WHERE team_id='{TEAM}';").splitlines()
    keys = ['auth:team:team-' + TEAM] + ['auth:team:' + h for h in hashes]
    # Same exact cache entries invalidated by official InvalidateTeamCache.
    p = subprocess.run(['docker', 'exec', 'ae-e2b-redis', 'redis-cli', '--raw', 'DEL', *keys],
                       text=True, capture_output=True, check=True)
    return {'selected_key_count': len(keys), 'deleted_count': int(p.stdout.strip())}

def enter():
    assert not STATE.exists(), 'An experiment quota owner already exists'
    before = limits()
    assert before == 20, before
    assert query(f"SELECT count(*) FROM public.addons WHERE id='{ADDON}';") == '0'
    record = {'team_id': TEAM, 'addon_id': ADDON, 'before': before, 'target': 65,
              'started_epoch': time.time(), 'authorized_by_user': True}
    STATE.write_text(json.dumps(record, indent=2))
    query(f"""BEGIN;
      INSERT INTO public.addons(id,team_id,name,description,extra_concurrent_sandboxes,added_by,valid_to)
      VALUES ('{ADDON}','{TEAM}','fig8-2m-full-20261109',
      'Temporary user-authorized N64 experiment: one source plus 64 retained children',45,
      '00000000-0000-0000-0000-000000000000',now()+interval '6 hours');
      COMMIT;""")
    record['after'] = limits()
    record['cache_invalidation'] = invalidate()
    assert record['after'] == 65, record
    (P / 'quota-during.json').write_text(json.dumps(record, indent=2))
    print('Test-team concurrency 20 -> 65; other teams and resource limits unchanged.', flush=True)

def leave():
    if not STATE.exists():
        return
    before = json.loads(STATE.read_text())
    assert before['addon_id'] == ADDON and before['team_id'] == TEAM
    query(f"DELETE FROM public.addons WHERE id='{ADDON}' AND team_id='{TEAM}';")
    record = dict(before, restored_epoch=time.time(), restored_limit=limits(),
                  cache_invalidation=invalidate())
    (P / 'quota-restored.json').write_text(json.dumps(record, indent=2))
    assert record['restored_limit'] == before['before'], record
    STATE.rename(P / 'quota-before.restored.json')
    print('Test-team concurrency restored to 20.', flush=True)

if __name__ == '__main__':
    {'enter': enter, 'leave': leave}[sys.argv[1]]()
