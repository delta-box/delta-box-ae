"""Experiment-only: prepare first request without executing an action.

The external controller owns request execution. The real step must reproduce
the exact request body before it may consume the response. No recorded future
request is used to prepare a prompt.
"""
import hashlib
import json
import os
import random
import time
from pathlib import Path


def body_hash(body):
    return hashlib.sha256(json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def install(g):
    import httpx
    original_send = httpx.Client.send
    original_handle = g['handle_command']
    cache = Path(os.environ['OVERLAP_CACHE'])
    mode = {'capture': False}

    class RequestCaptured(BaseException):
        def __init__(self, request):
            self.request = request

    def send(client, request, *args, **kwargs):
        if '/chat/completions' not in str(request.url):
            return original_send(client, request, *args, **kwargs)
        body = request.read()
        if mode['capture']:
            raise RequestCaptured({'url': str(request.url), 'body': body.decode(), 'sha256': body_hash(body)})
        if cache.exists():
            record = json.loads(cache.read_text())
            consumed = Path(record['consumed'])
            if not consumed.exists():
                actual = body_hash(body)
                if actual != record['request_sha256']:
                    raise RuntimeError('Prepared request does not match actual request: ' + actual + ' != ' + record['request_sha256'])
                result = json.loads(Path(record['response']).read_text())
                consumed.write_text(json.dumps({'request_sha256': actual, 't_ns': time.monotonic_ns()}))
                return httpx.Response(result['status'], content=result['body'].encode(), request=request, headers={'Content-Type': 'application/json'})
        return original_send(client, request, *args, **kwargs)

    httpx.Client.send = send

    def handle(command):
        if command.get('op') == 'quiesce':
            # The external mock is single-threaded and is serving inference.
            # Do not synchronously query its admin endpoint before the dump.
            # No process state changes are omitted; only diagnostic HTTP stats.
            return {'ok': True, 'quiesce': g['quiesce_for_criu'](),
                    'state': {k:g['CTX'].get(k) for k in ['instance','checkpoint_seq','node_id','phase']}}
        if command.get('op') != 'overlap_prepare':
            return original_handle(command)
        import numpy as np
        py_state, np_state = random.getstate(), np.random.get_state()
        request = None
        mode['capture'] = True
        try:
            tree = g['tree_from_dict'](command['tree'])
            node = tree.get_node_by_id(command['selected_node_id'])
            if node is not None and not tree.is_finished():
                expanded = tree._expand(node)
                if expanded is not None:
                    tree._simulate(expanded)
                    raise RuntimeError('Preparation reached action completion without intercepting a request')
        except RequestCaptured as captured:
            request = captured.request
        finally:
            mode['capture'] = False
            random.setstate(py_state)
            np.random.set_state(np_state)
            g['quiesce_for_criu']()
        return {'ok': True, 'request': request, 'state': g['state']()}

    g['handle_command'] = handle
