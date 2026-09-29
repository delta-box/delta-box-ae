"""Avoid reparsing identical trust roots for Cube's HTTPX transports."""
from contextlib import contextmanager
from functools import wraps
import inspect
import os
import threading


@contextmanager
def reuse_default_ssl_contexts():
    """Reuse default trust contexts, keyed by the transport's actual ALPN policy.

    Creation stays lazy in the first ordinary transport construction. HTTPX's
    original factory chooses trust roots and verification; custom certificates
    and verify settings pass through unchanged. Connection pools remain distinct.
    """
    import httpx
    from httpx._transports import default as transport

    original_init = transport.HTTPTransport.__init__
    factory = transport.create_ssl_context
    if getattr(original_init, '_cube_context_cache', False):
        raise RuntimeError('Cube TLS context cache is already active')
    signature = inspect.signature(original_init)
    factory_signature = inspect.signature(factory)
    if (not {'verify', 'cert', 'trust_env', 'http2'} <= set(signature.parameters)
            or not {'verify', 'cert', 'trust_env'} <= set(factory_signature.parameters)):
        yield {'policy': 'uncached: unsupported transport/factory signature',
               'httpx_version': httpx.__version__, 'active': False}
        return
    lock = threading.Lock()
    contexts = {}
    state = {'policy': 'lazy default trust contexts keyed by actual HTTP/ALPN policy; distinct pools',
             'httpx_version': httpx.__version__, 'active': True,
             'factory_calls': 0, 'cache_hits': 0, 'custom_calls': 0}

    @wraps(original_init)
    def initialize(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        if (values['verify'] is not True or values['cert'] is not None
                or type(values['http2']) is not bool or type(values['trust_env']) is not bool):
            with lock:
                state['custom_calls'] += 1
            return original_init(self, *args, **kwargs)
        environment = tuple(os.environ.get(name) for name in
                            ('SSL_CERT_FILE', 'SSL_CERT_DIR', 'SSLKEYLOGFILE'))
        # httpcore mutates ALPN protocols during connection setup. Separate
        # contexts for HTTP/1 and HTTP/2 transports prevent cross-client races.
        key = (values.get('http1', True), values['http2'], values['trust_env'], environment)
        with lock:
            if key in contexts:
                state['cache_hits'] += 1
            else:
                state['factory_calls'] += 1
                contexts[key] = factory(verify=True, cert=None, trust_env=values['trust_env'])
            bound.arguments['verify'] = contexts[key]
        return original_init(*bound.args, **bound.kwargs)

    initialize._cube_context_cache = True
    transport.HTTPTransport.__init__ = initialize
    try:
        yield state
    finally:
        transport.HTTPTransport.__init__ = original_init
