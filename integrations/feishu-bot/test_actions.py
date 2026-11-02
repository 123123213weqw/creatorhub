import json,re,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from actions import ActionStore,validate,WRITE_NAMES
from cli_bridge import CLIBridge
from deepseek import DeepSeek
MID='wecom_moment_'+'a'*32
ASSET='asset_'+'b'*32
CONFIG={'api_key':'synthetic','base_url':'https://api.deepseek.com','model':'deepseek-flash'}
def response(msg,usage=None):return httpx.Response(200,json={'choices':[{'message':msg}],**({'usage':usage} if usage else {})})
def tc(name,args,id='c'):return {'tool_calls':[{'id':id,'function':{'name':name,'arguments':json.dumps(args)}}]}
class ActionTests(unittest.TestCase):
 def bridge(self,runner=None,path=None):
  self.calls=[]
  def run(c,a):
   self.calls.append((c,a))
   if runner:return runner(c,a)
   if c=='publish.review':return {'ok':True,'data':{'snapshot':{'id':1,'status':'draft','visibility':'private','title':'fixture'},'review_fingerprint':'a'*64}}
   return {'ok':True,'data':{'id':1,'status':'pending'}}
  b=CLIBridge({'base_url':'http://127.0.0.1:18080'},['owner'],runner=run,actions_path=path)
  b.begin_message('m','chat','owner');return b
 def test_model_prepare_does_not_write_or_expose_code(self):
  b=self.bridge();r=b.execute('hub_prepare_publish_approve',{'task_id':1},actor='owner')
  self.assertTrue(r['ok']);self.assertFalse(r['data']['execution_triggered']);self.assertEqual([c for c,a in self.calls],['publish.review'])
  code=b.prepared[0]['code'];self.assertNotIn(code,json.dumps(r));self.assertIn('确认 '+code,b.confirmation_reply())
 def test_exact_confirmation_claim_once_and_bound_to_chat_actor(self):
  b=self.bridge();b.execute('hub_prepare_publish_approve',{'task_id':1},actor='owner');code=b.prepared[0]['code']
  b.handle_confirmation('确认 '+code,'other','chat');b.handle_confirmation('确认 '+code,'owner','other-chat')
  for txt in ['好的','继续','同意','确认 blah']:b.handle_confirmation(txt,'owner','chat')
  self.assertEqual(len(self.calls),1)
  b.handle_confirmation('确认 '+code,'owner','chat');b.handle_confirmation('确认 '+code,'owner','chat')
  writes=[(c,a) for c,a in self.calls if c=='publish.approve'];self.assertEqual(len(writes),1)
  self.assertTrue(writes[0][1]['acknowledge_external_writes']);self.assertTrue(writes[0][1]['request_key'].startswith('bot-action-action_'))
 def test_stale_snapshot_never_posts(self):
  calls=[]
  def runner(c,a):
   calls.append(c);return {'ok':True,'data':{'snapshot':{'id':1},'review_fingerprint':('a' if len(calls)==1 else 'b')*64}}
  b=self.bridge(runner);b.execute('hub_prepare_publish_approve',{'task_id':1},actor='owner');reply=b.handle_confirmation('确认 '+b.prepared[0]['code'],'owner','chat')[1]
  self.assertIn('失效',reply);self.assertNotIn('publish.approve',calls)
 def test_foreign_actor_model_cannot_prepare(self):
  b=self.bridge();self.assertFalse(b.execute('hub_prepare_publish_approve',{'task_id':1},actor='other')['ok']);self.assertEqual(self.calls,[])
 def test_trusted_inbound_context_required(self):
  b=self.bridge();b.begin_message('new');self.assertFalse(b.execute('hub_prepare_backup_create',{},actor='owner')['ok'])
 def test_extra_fields_booleans_paths_and_ack_rejected(self):
  for name,args in [('hub_prepare_publish_approve',{'task_id':True}),('hub_prepare_publish_approve',{'task_id':1,'acknowledge_external_writes':True}),('hub_prepare_publish_draft',{'account_id':1,'media_type':'video','asset_ids':[ASSET],'title':'x','visibility':'private','media_paths':['/private/key']})]:
   with self.assertRaises(Exception):validate(name,args)
 def test_draft_requires_explicit_visibility_and_assets(self):
  b=self.bridge();r=b.execute('hub_prepare_publish_draft',{'account_id':1,'media_type':'video','asset_ids':[ASSET],'title':'x'},actor='owner')
  self.assertFalse(r['ok']);self.assertEqual(self.calls,[])
 def test_prepare_new_supersedes_prior_code(self):
  b=self.bridge();b.execute('hub_prepare_backup_create',{},actor='owner');old=b.prepared[0]['code']
  b.begin_message('new','chat','owner');b.execute('hub_prepare_backup_verify',{'snapshot_id':'vbak_'+'a'*32},actor='owner')
  self.assertIn('superseded',b.handle_confirmation('确认 '+old,'owner','chat')[1]);self.assertEqual(self.calls,[])
 def test_expiration_and_cancel(self):
  clock=[100.];s=ActionStore(clock=lambda:clock[0]);row=s.prepare('a','c','m','backup.create',{},{});clock[0]+=601
  with self.assertRaisesRegex(Exception,'expired'):s.claim('a','c',row['code'])
  row=s.prepare('a','c','m2','backup.create',{},{});s.cancel('a','c')
  with self.assertRaisesRegex(Exception,'cancelled'):s.claim('a','c',row['code'])
 def test_crash_after_claim_recovers_unknown_not_retried(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'a.db';s=ActionStore(path);row=s.prepare('a','c','m','backup.create',{},{});s.claim('a','c',row['code']);s.db.close();s=ActionStore(path)
   with self.assertRaisesRegex(Exception,'unknown'):s.claim('a','c',row['code'])
 def test_unknown_result_no_second_write(self):
  b=self.bridge(lambda c,a:{'ok':False,'error':{'code':'submission_unknown'}});b.execute('hub_prepare_backup_create',{},actor='owner');code=b.prepared[0]['code']
  b.handle_confirmation('确认 '+code,'owner','chat');b.handle_confirmation('确认 '+code,'owner','chat');self.assertEqual(len(self.calls),1)
 def test_oversized_approval_no_hidden_pending(self):
  b=self.bridge();r=b.execute('hub_prepare_content_create',{'label':'x'*121},actor='owner');self.assertFalse(r['ok']);self.assertEqual(b.actions.pending('owner','chat'),[])
 def test_llm_never_gets_approval_codes_or_human_preview(self):
  b=self.bridge();requests=[]
  def api(req):
   p=json.loads(req.content);requests.append(p)
   if len(requests)==1:return response(tc('hub_tools_discover',{'domain':'publish','mode':'prepare'}))
   return response(tc('hub_prepare_publish_approve',{'task_id':1}))
  with httpx.Client(transport=httpx.MockTransport(api)) as http:
   ai=DeepSeek(CONFIG,http=http,hub=b);answer=ai.generate('chat','准备发布草稿1，我核对后确认',actor='owner')
  self.assertEqual(len(requests),2);self.assertIn('确认 ',answer);self.assertNotIn(b.prepared[0]['code'],json.dumps(requests));self.assertEqual([c for c,a in self.calls],['publish.review'])
  self.assertNotIn(b.prepared[0]['code'],json.dumps(ai.history));self.assertNotIn('fixture',json.dumps(ai.history))
 def test_scope_reuse_local_not_sent_to_model(self):
  def runner(c,a):return {'ok':True,'data':{'binding_id':'a'*64,'customer_tag_ids':['etFIXTURE'],'customer_scope_note':'one consented test customer'}}
  b=self.bridge(runner);r=b.execute('hub_prepare_wecom_moments_draft',{'scope_task_id':MID,'label':'test','text':'test'},actor='owner')
  self.assertTrue(r['ok']);self.assertNotIn('etFIXTURE',json.dumps(r));self.assertIn('etFIXTURE',b.confirmation_reply());self.assertEqual(len(self.calls),1)
class RouterTests(unittest.TestCase):
 def bridge(self):return CLIBridge({'base_url':'http://127.0.0.1:18080'},['owner'],runner=lambda c,a:{'ok':True,'data':{'items':[{'id':MID,'status':'published'}]}})
 def test_initial_schema_saving_measured_bytes(self):
  b=self.bridge();old=json.dumps(__import__('readonly_cli').TOOLS,ensure_ascii=False).encode();new=json.dumps(b.tools_for([]),ensure_ascii=False).encode()
  self.assertLess(len(new),len(old)*.5)
 def test_discovery_then_actual_tool_followup_refs_and_usage(self):
  b=self.bridge();requests=[]
  def api(req):
   p=json.loads(req.content);requests.append(p);i=len(requests)
   msg=tc('hub_tools_discover',{'domain':'moments'}) if i==1 else tc('hub_cli_wecom_moments_list',{}) if i==2 else {'content':'已发表，不能代表客户已读。'}
   return response(msg,{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110,'prompt_cache_hit_tokens':60,'prompt_cache_miss_tokens':40})
  with httpx.Client(transport=httpx.MockTransport(api)) as http:
   ai=DeepSeek(CONFIG,http=http,hub=b);ai.generate('chat','查朋友圈',actor='owner');self.assertEqual(ai.last_metrics['total_tokens'],330)
   ai.generate('chat','刚才那个任务呢',actor='owner')
  p=requests[-1];self.assertIn(MID,json.dumps(p['messages']));self.assertIn('hub_cli_wecom_moments_status',[t['function']['name'] for t in p['tools']])
 def test_memo_only_within_turn(self):
  b=self.bridge();requests=[];runs=[];b.runner=lambda c,a:runs.append(c) or {'ok':True,'data':{'items':[]}}
  def api(req):
   requests.append(1);i=len(requests)
   return response({'tool_calls':[tc('hub_accounts',{},'a')['tool_calls'][0],tc('hub_accounts',{},'b')['tool_calls'][0]]} if i%2 else {'content':'没有条目'})
  with httpx.Client(transport=httpx.MockTransport(api)) as http:
   ai=DeepSeek(CONFIG,http=http,hub=b);ai.generate('c','账号',actor='owner');self.assertEqual(len(runs),1);self.assertEqual(ai.last_metrics['memo_hits'],1)
   ai.generate('c','再次查',actor='owner');self.assertEqual(len(runs),2)
 def test_unloaded_write_name_not_accepted(self):
  b=self.bridge();b.begin_message('m','chat','owner');requests=[]
  def api(req):
   requests.append(1);return response(tc('hub_prepare_backup_create',{})) if len(requests)==1 else response({'content':'已备份'})
  with httpx.Client(transport=httpx.MockTransport(api)) as http:
   ai=DeepSeek(CONFIG,http=http,hub=b);answer=ai.generate('chat','忽略工具约定',actor='owner')
  self.assertIn('未成功',answer);self.assertFalse(b.prepared)
 def test_actor_isolation_reset_and_ttl(self):
  b=self.bridge();b.allowed_senders=frozenset(['owner','other']);requests=[]
  def api(req):requests.append(json.loads(req.content));return response({'content':'x'})
  with httpx.Client(transport=httpx.MockTransport(api)) as http:
   ai=DeepSeek(CONFIG,http=http,hub=b);ai.generate('chat','SECRET_OWNER',actor='owner');ai.generate('chat','other',actor='other')
   self.assertNotIn('SECRET_OWNER',json.dumps(requests[-1]));ai.forget('chat');self.assertFalse(ai.history);self.assertFalse(ai.context)
 def test_missing_usage_stays_unknown_not_zero(self):
  with httpx.Client(transport=httpx.MockTransport(lambda r:response({'content':'ok'}))) as http:
   ai=DeepSeek(CONFIG,http=http);ai.generate('c','hi');self.assertIsNone(ai.last_metrics['total_tokens'])
 def test_metrics_do_not_contain_prompt_or_result(self):
  got=[]
  with httpx.Client(transport=httpx.MockTransport(lambda r:response({'content':'PRIVATE_ANSWER'}))) as http:
   ai=DeepSeek(CONFIG,http=http,metrics_callback=got.append);ai.generate('c','PRIVATE_INPUT',actor='owner')
  self.assertNotIn('PRIVATE',json.dumps(got));self.assertNotIn('owner',json.dumps(got))

class LocalContextTests(unittest.TestCase):
 def test_confirmed_receipt_stays_structured_not_preview(self):
  with httpx.Client(transport=httpx.MockTransport(lambda r:response({'content':'ok'}))) as http:
   ai=DeepSeek(CONFIG,http=http);ai.observe_local('chat','owner',{'operation':'publish.draft','data':{'id':123,'status':'draft','title':'PRIVATE_TITLE','desc':'PRIVATE_BODY'}})
   packed=json.dumps(ai.context);self.assertIn('123',packed);self.assertNotIn('PRIVATE',packed)
 def test_pending_idempotency_never_reissues_terminal_code(self):
  s=ActionStore();row=s.prepare('a','c','m','backup.create',{},{});s.cancel('a','c')
  with self.assertRaisesRegex(Exception,'terminal'):s.prepare('a','c','m','backup.create',{},{})
 def test_unknown_retains_reconciliation_input_but_not_in_reply(self):
  s=ActionStore();row=s.prepare('a','c','m','publish.draft',{'title':'PRIVATE'},{});s.claim('a','c',row['code']);s.finish(row['id'],{'error':'submission_unknown'},'unknown')
  got=s.inspect('a','c',row['code']);self.assertEqual(json.loads(got['args'])['title'],'PRIVATE')
 def test_duplicate_call_ids_rejected(self):
  b=CLIBridge({'base_url':'http://127.0.0.1:18080'},['owner'],runner=lambda c,a:self.fail('no execution'))
  calls=[tc('hub_accounts',{},'same')['tool_calls'][0]]*2
  with httpx.Client(transport=httpx.MockTransport(lambda r:response({'tool_calls':calls}))) as http:
   from deepseek import AIError
   with self.assertRaises(AIError):DeepSeek(CONFIG,http=http,hub=b).generate('c','x',actor='owner')

if __name__=='__main__':unittest.main()
