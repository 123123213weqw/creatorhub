import io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from creatorhub_cli.client import Client,Error,CATALOG,Transport,validate
from creatorhub_cli.operations import CATALOG as OPS

class Fake:
 def __init__(self):self.calls=[]
 def get(self,url):self.calls.append(('GET',url));return {'items':[]}
 def post(self,url,body,key):self.calls.append(('POST',url,body,key));return {'id':'content_'+'a'*32,'state':'created','file_path':'PRIVATE','token':'PRIVATE'}

class OperationsTests(unittest.TestCase):
 def test_fixed_commands_no_grants_approvals_dispatch_or_config(self):
  self.assertEqual(len(OPS),26)
  for name in ['team.grant','team.credential','delivery.dispatch','delivery.resolve','delivery.config','proof.review','manual.review']:self.assertNotIn(name,CATALOG)
  c=Client(transport=Fake())
  for route in ['/api/operations/actors','/api/operations/proofs/proof_'+'a'*32+'/review','/api/operations/delivery/config']:
   with self.assertRaises(Error):c._post(route,{},'fixture-key-00000001')
 def test_structured_target_schema_and_strict_identity(self):
  args={'version_id':'version_'+'a'*32,'employee_id':'actor_'+'b'*32,'due_at':'2026-11-02T10:00:00Z','targets':[{'channel':'manual','label':'Fixture'}],'request_key':'fixture-key-00000001'}
  self.assertEqual(validate('distribution.create',args),args)
  for target in [{'channel':'manual','label':'Fixture','actor':'admin'},{'channel':'dm','label':'Fixture'},'Fixture']:
   with self.assertRaises(Error):validate('distribution.create',{**args,'targets':[target]})
 def test_operations_client_shares_hub_and_sanitizes(self):
  f=Fake();r=Client(transport=f).call('content.create',{'label':'Fixture','request_key':'fixture-key-00000001'})
  self.assertEqual(f.calls[0][1],'http://127.0.0.1:18080/api/operations/contents')
  self.assertNotIn('PRIVATE',json.dumps(r));self.assertFalse(r['external_write_triggered'])
 def test_team_scope_no_silent_admin_fallback(self):
  f=Fake();f.token='cht_'+'a'*32;c=Client(transport=f)
  with self.assertRaises(Error) as e:c.call('accounts.list')
  self.assertEqual(e.exception.code,'team_scope');self.assertFalse(f.calls)
  c.call('team.session');self.assertEqual(len(f.calls),1)
 def test_invalid_team_token_no_network(self):
  with patch.dict(os.environ,{'HUBCTL_OPERATIONS_TOKEN':'secret\r\nInjected: x'}),self.assertRaises(Error):Transport()
 def test_proof_output_metadata_only(self):
  f=Fake();f.get=lambda url:{'id':'proof_'+'a'*32,'note':'PRIVATE_CONTACT','file_path':'PRIVATE','token':'PRIVATE','sha256':'a'*64,'state':'submitted'}
  r=Client(transport=f).call('proof.show',{'proof_id':'proof_'+'a'*32});self.assertNotIn('PRIVATE',json.dumps(r))
 def test_no_multipart_retry_or_filename_leak(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'PRIVATE_CONTACT.png';p.write_bytes(b'fixture image')
   opener=Mock();context=Mock();context.__enter__=Mock(return_value=io.BytesIO(b'{"id":"fixture"}'));context.__exit__=Mock(return_value=False);opener.open.return_value=context
   with patch('urllib.request.build_opener',return_value=opener):Transport().multipart('http://127.0.0.1:18080/api/operations/targets/target_'+'a'*32+'/proofs',str(p),{'note':'Fixture','revision':'1'},'fixture-key-00000001')
   request=opener.open.call_args.args[0];self.assertNotIn(b'PRIVATE_CONTACT',request.data);self.assertEqual(opener.open.call_count,1)
