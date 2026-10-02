"""Exercise installed SDK request construction against a network-forbidden transport."""
import json
from pathlib import Path
import subprocess
import unittest

SDK = Path('/mnt/disk2/dyp/ae-hosted-20260922/e2b/sdk-venv/bin/python')
PROGRAM = "import json,socket,httpcore\nfrom unittest.mock import patch\nfrom types import SimpleNamespace\nfrom e2b import Sandbox\nfrom e2b.connection_config import ConnectionConfig\nfrom e2b.sandbox_sync.main import SandboxApi\nfrom packaging.version import Version\ncaptured=[]\ndef handle(pool, request):\n captured.append({'method':request.method.decode(),'url':str(request.url),'headers':{k.decode().lower():v.decode() for k,v in request.headers}})\n raise RuntimeError('offline transport captured; no network')\nopts={'api_url':'http://127.0.0.1:3100','sandbox_url':'http://127.0.0.1:3102','api_key':'fixture-key-not-secret'}\nresponse=SimpleNamespace(sandbox_id='fixture-sandbox',sandbox_domain='localhost',envd_version='0.6.1',envd_access_token='fixture-access-not-secret',traffic_access_token=None)\nwith patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')),patch.object(httpcore.ConnectionPool,'handle_request',handle),patch.object(SandboxApi,'_create_sandbox',return_value=response) as create_api:\n original=Sandbox.create(template='fixture',**opts)\n missing=Sandbox(sandbox_id=original.sandbox_id,sandbox_domain=original.sandbox_domain,envd_version=original._envd_version,envd_access_token=original._envd_access_token,connection_config=ConnectionConfig(**opts,request_timeout=2))\n repaired=Sandbox(sandbox_id=original.sandbox_id,sandbox_domain=original.sandbox_domain,envd_version=original._envd_version,envd_access_token=original._envd_access_token,connection_config=ConnectionConfig(**opts,request_timeout=2,extra_sandbox_headers=dict(original.connection_config.sandbox_headers)))\n for sb in [original,missing,repaired]:\n  try:sb.commands.run('fixture-read-only-probe',timeout=2,request_timeout=2)\n  except RuntimeError:pass\n  except Exception as error: print('SDK_ERROR',type(error).__name__)\n assert len(captured)==3,len(captured)\n required={'e2b-sandbox-id','e2b-sandbox-port','x-access-token'}\n assert required<=set(captured[0]['headers'])\n assert not required & set(captured[1]['headers'])\n assert all(captured[0]['headers'][k]==captured[2]['headers'][k] for k in required)\n print(json.dumps({'status':'verified-offline-real-sdk','header_keys':[sorted(x['headers']) for x in captured],'requests':len(captured),'mutating_api_network_calls':0,'original_create_api_stub_calls':create_api.call_count,'repaired_headers_equal_original':True}))\n"


class RealSDKHeadersTests(unittest.TestCase):
    @unittest.skipUnless(SDK.is_file(), 'Hosted SDK runtime is not installed on this host')
    def test_real_sdk_headers_preserved_without_network(self):
        result = subprocess.run([str(SDK), '-I', '-c', PROGRAM], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['status'], 'verified-offline-real-sdk')
        self.assertEqual(receipt['requests'], 3)
        self.assertEqual(receipt['mutating_api_network_calls'], 0)
        self.assertTrue(receipt['repaired_headers_equal_original'])


if __name__ == '__main__':
    unittest.main()
