import asyncio
import importlib.util
import json
import ssl
import struct
import time
import unittest
from pathlib import Path
from unittest.mock import patch
try:
    import httpx
    from cubesandbox import Sandbox
    from cubesandbox._config import Config
except ImportError:
    HAVE_SDK = False
else:
    HAVE_SDK = True

SOURCE=Path(__file__).resolve().parents[2]/'ae/runners/cube_envd_ready.py'
if HAVE_SDK and SOURCE.exists():
    spec=importlib.util.spec_from_file_location('cube_envd_ready',SOURCE)
    gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
else:
    gate=None
UUID='12345678-1234-1234-1234-123456789abc'

@unittest.skipUnless(HAVE_SDK, 'Cube SDK and HTTPX are optional; set SDK PYTHONPATH to run')
class EnvdReadinessTests(unittest.TestCase):
    def setup_case(self,statuses,*,body=UUID,command_error=False):
        calls=[];remaining=list(statuses);budgets=[]
        def handler(request):
            calls.append((request.method,request.url.path))
            if request.method=='GET':
                self.assertEqual(request.url.path,'/files')
                self.assertEqual(request.url.params['path'],'/proc/sys/kernel/random/boot_id')
                self.assertEqual(request.url.params['username'],'root')
                self.assertEqual(request.headers['X-Access-Token'],'private-test-token')
                value=remaining.pop(0) if len(remaining)>1 else remaining[0]
                if isinstance(value,Exception):raise value
                return httpx.Response(value,text=body if value==200 else 'not ready')
            self.fail('Readiness must not submit a command')
        sb=Sandbox({'sandboxID':'test-id','templateID':'test-template','envdAccessToken':'private-test-token'},Config(api_url='http://127.0.0.1:1',proxy_node_ip='127.0.0.1'))
        client=httpx.Client(transport=httpx.MockTransport(handler));self.addCleanup(client.close);sb._client=client
        def execute(sandbox,command,timeout):
            calls.append(('EXECUTE',command));budgets.append(timeout)
            if not any(c[0]=='GET' for c in calls):raise RuntimeError('Connection refused before envd readiness')
            if command_error:raise RuntimeError('command stream failed after submission')
            return 'command output'
        return sb,calls,budgets,execute,handler
    def invoke(self,sb,execute,handler,timeout,records):
        if gate is None:return execute(sb,'one command',timeout)
        with patch.object(gate,'_async_transport',lambda:httpx.MockTransport(handler)):
            return gate.command_when_ready(sb,'one command',timeout,execute,records.append)
    def test_waits_on_readonly_endpoint_then_executes_once_with_remaining_budget(self):
        sb,calls,budgets,execute,handler=self.setup_case([502,503,200]);records=[];error=None
        try:result=self.invoke(sb,execute,handler,1,records)
        except Exception as exc:error=str(exc)
        self.assertIsNone(error,error);self.assertEqual(result,'command output')
        self.assertEqual(len(budgets),1);self.assertLess(budgets[0],1)
        self.assertEqual([c[0] for c in calls],['GET','GET','GET','EXECUTE'])
        self.assertTrue(records[0]['ok']);self.assertEqual(records[0]['attempts'],3)
        self.assertNotIn('private-test-token',json.dumps(records))
    def test_connection_refused_is_retried_only_as_read(self):
        sb,calls,budgets,execute,handler=self.setup_case([httpx.ConnectError('refused'),200]);records=[]
        self.assertEqual(self.invoke(sb,execute,handler,1,records),'command output')
        self.assertEqual([c[0] for c in calls],['GET','GET','EXECUTE'])
    def test_auth_and_unknown_path_fail_without_command_or_retry(self):
        for status in (401,403,404,500):
            with self.subTest(status=status):
                sb,calls,budgets,execute,handler=self.setup_case([status,200]);records=[]
                with self.assertRaisesRegex(RuntimeError,str(status)):self.invoke(sb,execute,handler,1,records)
                self.assertEqual(calls,[('GET','/files')]);self.assertFalse(records[0]['ok'])
    def test_wrong_success_body_is_not_readiness(self):
        sb,calls,budgets,execute,handler=self.setup_case([200],body='<html>proxy page</html>');records=[]
        with self.assertRaisesRegex(RuntimeError,'boot ID'):self.invoke(sb,execute,handler,1,records)
        self.assertEqual(calls,[('GET','/files')])
    def test_deadline_exhaustion_never_submits_command(self):
        sb,calls,budgets,execute,handler=self.setup_case([502]);records=[];before=time.monotonic()
        with self.assertRaises(TimeoutError):self.invoke(sb,execute,handler,.025,records)
        self.assertFalse(budgets);self.assertGreaterEqual(time.monotonic()-before,.02)
        self.assertFalse(records[0]['ok'])
    def test_command_failure_is_not_retried(self):
        sb,calls,budgets,execute,handler=self.setup_case([200],command_error=True);records=[]
        with self.assertRaisesRegex(RuntimeError,'after submission'):self.invoke(sb,execute,handler,1,records)
        self.assertEqual([c[0] for c in calls],['GET','EXECUTE']);self.assertEqual(len(budgets),1)
    def test_actual_sdk_connect_command_follows_readiness_probe(self):
        sb,calls,budgets,_,_=self.setup_case([200]);records=[];requests=[]
        def frame(value,flag=0):
            body=json.dumps(value).encode();return bytes([flag])+struct.pack('>I',len(body))+body
        def handler(request):
            requests.append((request.method,request.url.path))
            if request.method=='GET':
                self.assertEqual(request.headers['Host'],sb.get_host(49983))
                return httpx.Response(503 if len(requests)==1 else 200,text=UUID)
            self.assertEqual(request.url.path,'/process.Process/Start')
            self.assertEqual(request.headers['X-Access-Token'],'private-test-token')
            self.assertIn('Basic ',request.headers['Authorization'])
            return httpx.Response(200,stream=httpx.ByteStream(frame({'event':{'data':{'stdout':'b2s='}}})+frame({'event':{'end':{'exitCode':0}}})+frame({},2)))
        client=httpx.Client(transport=httpx.MockTransport(handler));self.addCleanup(client.close);sb._client=client
        result=self.invoke(sb,lambda sandbox,command,timeout:sandbox.commands.run(command,timeout=timeout).stdout,handler,1,records)
        self.assertEqual(result,'ok');self.assertEqual(requests,[('GET','/files'),('GET','/files'),('POST','/process.Process/Start')])
    def test_invalid_deadline_has_no_network_or_execution(self):
        for timeout in (0,-1,float('inf'),float('nan')):
            sb,calls,budgets,execute,handler=self.setup_case([200]);records=[]
            with self.assertRaises(ValueError):self.invoke(sb,execute,handler,timeout,records)
            self.assertFalse(calls)
    def test_callback_time_is_removed_from_command_budget(self):
        sb,calls,budgets,execute,handler=self.setup_case([200]);records=[]
        def record(event):records.append(event);time.sleep(.03)
        with patch.object(gate,'_async_transport',lambda:httpx.MockTransport(handler)):
            with self.assertRaises(TimeoutError):gate.command_when_ready(sb,'cmd',.02,execute,record)
        self.assertFalse(budgets)
    def test_slow_response_body_is_cancelled_and_closed_at_deadline(self):
        sb,calls,budgets,execute,_=self.setup_case([200]);records=[]
        class SlowBody(httpx.AsyncByteStream):
            closed=False
            async def __aiter__(self):
                for chunk in (UUID[:12],UUID[12:24],UUID[24:]):
                    await asyncio.sleep(.02);yield chunk.encode()
            async def aclose(self):self.closed=True
        body=SlowBody()
        def handler(request):return httpx.Response(200,stream=body)
        with self.assertRaises(TimeoutError):self.invoke(sb,execute,handler,.03,records)
        self.assertFalse(budgets);self.assertTrue(body.closed);self.assertFalse(records[0]['ok'])
    def test_large_readiness_body_is_rejected(self):
        sb,calls,budgets,execute,handler=self.setup_case([200],body='x'*129);records=[]
        with self.assertRaisesRegex(RuntimeError,'too large'):self.invoke(sb,execute,handler,1,records)
        self.assertFalse(budgets)
    def test_lazy_client_construction_exhaustion_prevents_start(self):
        sb,calls,budgets,execute,handler=self.setup_case([200]);records=[];client=sb._client;sb._client=None
        def build():time.sleep(.03);return client
        sb._build_data_client=build
        with self.assertRaises(TimeoutError):self.invoke(sb,execute,handler,.02,records)
        self.assertFalse(calls);self.assertFalse(budgets);self.assertIs(sb._client,client)

    def test_transport_preserves_certificate_validation(self):
        transport=gate._async_transport()
        try:
            context=transport._pool._ssl_context
            self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(context.check_hostname)
            self.assertEqual(transport._cube_context_mode,'shared-verified-context')
        finally:asyncio.run(transport.aclose())

if __name__=='__main__':unittest.main()
