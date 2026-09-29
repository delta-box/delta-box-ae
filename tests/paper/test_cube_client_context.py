"""Constructor and local protocol checks; no real Cube sandbox is launched."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import ssl
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ae.runners.cube_client_context import reuse_default_ssl_contexts

class ContextCacheTests(unittest.TestCase):
    def setUp(self):
        try:
            from httpx._transports import default
        except ImportError:
            self.skipTest('HTTPX is an optional Cube SDK dependency')
        self.transport=default

    def test_one_creation_under_parallel_default_calls_and_distinct_pools(self):
        contexts=[]
        def factory(verify=True,cert=None,trust_env=True,http2=False):
            if isinstance(verify,ssl.SSLContext):return verify
            time.sleep(.005)
            result=ssl.create_default_context();contexts.append(result);return result
        original_init=self.transport.HTTPTransport.__init__
        with patch.object(self.transport,'create_ssl_context',factory):
            with reuse_default_ssl_contexts() as state:
                with ThreadPoolExecutor(max_workers=16) as pool:
                    clients=list(pool.map(lambda _:self.transport.HTTPTransport(),range(16)))
                results=[client._pool._ssl_context for client in clients]
                self.assertEqual(len(contexts),1)
                self.assertTrue(all(result is results[0] for result in results))
                self.assertEqual(len({id(client._pool) for client in clients}),16)
                self.assertEqual(results[0].verify_mode,ssl.CERT_REQUIRED)
                self.assertTrue(results[0].check_hostname)
                self.assertEqual(state['cache_hits'],15)
                for client in clients:client.close()
            self.assertIs(self.transport.HTTPTransport.__init__,original_init)

    def test_custom_policy_delegates_and_environment_changes_do_not_alias(self):
        calls=[]
        def factory(verify=True,cert=None,trust_env=True,http2=False):
            if isinstance(verify,ssl.SSLContext):return verify
            calls.append((verify,cert,trust_env,http2));return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        def context(**kwargs):
            with self.transport.HTTPTransport(**kwargs) as client:return client._pool._ssl_context
        with patch.object(self.transport,'create_ssl_context',factory), reuse_default_ssl_contexts() as state:
            first=context()
            self.assertIs(first,context(verify=True))
            with patch.dict(os.environ,{'SSL_CERT_FILE':'/different-ca.pem'}):
                self.assertIsNot(first,context())
            self.assertIsNot(context(verify=False),context(verify=False))
            self.assertIsNot(context(cert='client.pem'),context(cert='client.pem'))
            self.assertEqual(state['custom_calls'],4)
        self.assertEqual(calls[-1][1],'client.pem')

    def test_actual_factory_preserves_trust_and_hostname_policy(self):
        with self.transport.HTTPTransport() as client:baseline=client._pool._ssl_context
        with reuse_default_ssl_contexts() as state:
            with self.transport.HTTPTransport() as client:shared=client._pool._ssl_context
            with self.transport.HTTPTransport() as client:self.assertIs(shared,client._pool._ssl_context)
            self.assertEqual(state['factory_calls'],1)
            for name in ('verify_mode','verify_flags','check_hostname','minimum_version','options'):
                self.assertEqual(getattr(shared,name),getattr(baseline,name))
            self.assertEqual(shared.get_ca_certs(binary_form=True),baseline.get_ca_certs(binary_form=True))

    def test_actual_http2_policy_does_not_share_alpn_context(self):
        with reuse_default_ssl_contexts() as state:
            with self.transport.HTTPTransport(http2=False) as client:first=client._pool._ssl_context
            with self.transport.HTTPTransport(http2=True) as client:second=client._pool._ssl_context
            with self.transport.HTTPTransport(http2=True) as client:self.assertIs(second,client._pool._ssl_context)
            self.assertIsNot(first,second)
            self.assertEqual(state['factory_calls'],2)

    def test_failure_does_not_cache_or_leave_global_override(self):
        calls=[]
        def factory(verify=True,cert=None,trust_env=True,http2=False):
            calls.append(1);raise ValueError('invalid trust roots')
        original_init=self.transport.HTTPTransport.__init__
        with patch.object(self.transport,'create_ssl_context',factory):
            with self.assertRaisesRegex(ValueError,'invalid trust roots'):
                with reuse_default_ssl_contexts():self.transport.HTTPTransport()
            self.assertIs(self.transport.HTTPTransport.__init__,original_init)
            self.assertEqual(len(calls),1)

    def test_unreviewed_factory_signature_keeps_original_behavior(self):
        def factory(verify=True):return object()
        original_init=self.transport.HTTPTransport.__init__
        with patch.object(self.transport,'create_ssl_context',factory):
            with reuse_default_ssl_contexts() as state:
                self.assertFalse(state['active'])
                self.assertIs(self.transport.HTTPTransport.__init__,original_init)

    def test_nested_scope_is_rejected(self):
        with reuse_default_ssl_contexts():
            with self.assertRaisesRegex(RuntimeError,'already active'):
                with reuse_default_ssl_contexts():pass

class LocalCubeProtocolTests(unittest.TestCase):
    def setUp(self):
        sdk=os.environ.get('CUBE_SDK_TEST_PATH')
        if not sdk:self.skipTest('Set CUBE_SDK_TEST_PATH for the installed-SDK contract test')
        sys.path.insert(0,sdk)
        from cubesandbox._commands import Commands
        from cubesandbox._transport import build_client
        self.Commands=Commands;self.build_client=build_client

    def test_parallel_commands_keep_host_routing_and_end_events(self):
        from types import SimpleNamespace
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']))
                payload=json.loads(raw[5:]);received.append((self.headers['Host'],payload))
                packets=[{'event':{'data':{'stdout':base64.b64encode(b'OK token=test bytes=67108864 checksum=2031729 requests=2').decode()}}},
                         {'event':{'end':{'exitCode':0}}}]
                body=b''.join(b'\0'+struct.pack('>I',len(b))+b for b in [json.dumps(p).encode() for p in packets])
                self.send_response(200);self.send_header('Content-Type','application/connect+json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        class Server(ThreadingHTTPServer):request_queue_size=64
        server=Server(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            config=SimpleNamespace(proxy_node_ip='127.0.0.1',proxy_port=server.server_port,request_timeout=5)
            with reuse_default_ssl_contexts() as state:
                def command(i):
                    sandbox=SimpleNamespace(_config=config,_client=None,_data={},get_host=lambda _:f'sandbox-{i}.cube.test')
                    sandbox._build_data_client=lambda:self.build_client(config)
                    try:
                        # Explicitly exercise the fallback used by the hosted Cube environment.
                        return self.Commands(sandbox)._run_with_connect_fallback('verify',timeout=5,cwd=None,envs={},user='root')
                    finally:
                        if sandbox._client is not None:sandbox._client.close()
                with ThreadPoolExecutor(max_workers=16) as pool:results=list(pool.map(command,range(16)))
                self.assertEqual(state['factory_calls'],1)
                self.assertEqual(state['cache_hits'],15)
            self.assertEqual(len(received),16)
            self.assertEqual({host for host,_ in received},{f'sandbox-{i}.cube.test' for i in range(16)})
            self.assertTrue(all(payload['process']['args']==['-l','-c','verify'] for _,payload in received))
            self.assertTrue(all(r.exit_code==0 and 'bytes=67108864' in r.stdout for r in results))
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
