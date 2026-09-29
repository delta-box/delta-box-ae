"""Read-only Cube envd readiness within the original command timing window."""
import asyncio
import math
import ssl
import time
import uuid

import httpx


def _async_transport():
    # Reuse the verified context selected by Cube's existing sync-transport
    # cache. The temporary pool has no connections and is always closed.
    # Unknown HTTPX internals fall back to normal certificate verification.
    with httpx.HTTPTransport() as configured:
        context = getattr(getattr(configured, '_pool', None), '_ssl_context', None)
    verify = context if isinstance(context, ssl.SSLContext) else True
    transport = httpx.AsyncHTTPTransport(verify=verify, retries=0, http1=True, http2=False)
    transport._cube_context_mode = 'shared-verified-context' if isinstance(context, ssl.SSLContext) else 'default-verified-fallback'
    return transport


async def _wait_for_envd(sandbox, deadline, record):
    from cubesandbox._commands import _envd_rpc_base_url_and_headers, DEFAULT_ENVD_USER
    base_url, headers = _envd_rpc_base_url_and_headers(sandbox)
    transport = _async_transport()
    record['probe_context_mode'] = getattr(transport, '_cube_context_mode', 'test-transport')
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Cube envd readiness exhausted command timeout')
            record['attempts'] += 1
            try:
                async with client.stream(
                    'GET', base_url + '/files',
                    params={'path': '/proc/sys/kernel/random/boot_id',
                            'username': DEFAULT_ENVD_USER},
                    headers=headers, timeout=min(1.0, remaining)) as response:
                    record['last_http_status'] = response.status_code
                    if response.status_code == 200:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 128:
                                raise RuntimeError('Cube envd boot ID response is too large')
                        try:
                            uuid.UUID(body.decode('ascii').strip())
                        except (ValueError, UnicodeError) as error:
                            raise RuntimeError('Cube envd returned an invalid boot ID') from error
                        return
                    if response.status_code not in (502, 503, 504):
                        raise RuntimeError(f'Cube envd readiness failed: HTTP {response.status_code}')
            except (httpx.ConnectError, httpx.ConnectTimeout,
                    httpx.ReadTimeout, httpx.RemoteProtocolError) as error:
                record['last_transport_error'] = type(error).__name__
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(.01, remaining))


def command_when_ready(sandbox, command, timeout, execute, record_event):
    """Probe envd with a total deadline, then submit the command exactly once.

    The original source/verification timer includes this entire call. Only the
    read-only GET repeats; authentication/protocol/command failures do not.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Cube command timeout must be positive and finite')
    start = time.monotonic()
    deadline = start + timeout
    record = {'operation': 'envd_readiness', 'sandbox_id': sandbox.sandbox_id,
              'started_ns': time.time_ns(), 'attempts': 0, 'ok': False,
              'path': '/files?path=/proc/sys/kernel/random/boot_id',
              'retry_scope': 'read-only GET; command is submitted once'}

    async def bounded_probe():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Cube envd readiness exhausted command timeout')
        await asyncio.wait_for(_wait_for_envd(sandbox, deadline, record), remaining)

    try:
        if sandbox._client is None:
            sandbox._client = sandbox._build_data_client()
        asyncio.run(bounded_probe())
        record['ok'] = True
    finally:
        record['ended_ns'] = time.time_ns()
        record['ms'] = (time.monotonic() - start) * 1000
        record_event(record)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Cube envd readiness exhausted command timeout')
    return execute(sandbox, command, remaining)
