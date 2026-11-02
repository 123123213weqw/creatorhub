import copy,json,unittest
from creatorhub_cli.client import Client,Error
from creatorhub_cli.bridge import process
MID='wecom_moment_'+'a'*32
KEY='confirmed-action-fixture-key'
class Fake:
 token=''
 def __init__(self,moments=False):
  self.calls=[];self.row={'id':MID if moments else 1,'status':'draft','demo_only':False,'review_required':True,'review_version':1,'review_digest':'a'*64,'digest':'b'*64,'title':'fixture','desc':'body','visibility':'private','text':'test','customer_tag_ids':['etFIXTURE'],'customer_scope_note':'controlled','media':[{'sha256':'c'*64,'size':1,'path':'NEVER','preview_url':'NEVER'}]}
 def get(self,url):self.calls.append(('GET',url));return copy.deepcopy(self.row)
 def post(self,url,body,key):
  self.calls.append(('POST',url,body,key));self.row['status']='pending' if url.endswith('/approve') else 'approved' if url.endswith('/review') else 'awaiting_member';return copy.deepcopy(self.row)
class ConfirmedTests(unittest.TestCase):
 def test_review_snapshot_exact_but_not_paths(self):
  f=Fake();r=Client(transport=f).call('publish.review',{'task_id':1})
  self.assertEqual(r['snapshot']['desc'],'body');self.assertNotIn('NEVER',json.dumps(r));self.assertEqual(len(r['review_fingerprint']),64)
 def test_approve_requires_ack_and_exact_fingerprint(self):
  f=Fake();c=Client(transport=f);r=c.call('publish.review',{'task_id':1});a={'task_id':1,'review_fingerprint':r['review_fingerprint'],'request_key':KEY,'acknowledge_external_writes':False}
  with self.assertRaisesRegex(Error,'需要明确确认'):c.call('publish.approve',a)
  self.assertFalse(any(x[0]=='POST' for x in f.calls));a['acknowledge_external_writes']=True
  r=c.call('publish.approve',a);self.assertEqual(r['status'],'pending');self.assertNotIn('body',json.dumps(r))
  self.assertEqual(f.calls[-1][2],{'review_version':1,'review_digest':'a'*64})
 def test_content_visibility_version_or_media_change_invalidates(self):
  for key,value in [('desc','changed'),('visibility','public'),('review_version',2),('media',[{'sha256':'d'*64,'size':1}])]:
   f=Fake();c=Client(transport=f);r=c.call('publish.review',{'task_id':1});f.row[key]=value
   with self.assertRaises(Error) as ctx:c.call('publish.approve',{'task_id':1,'review_fingerprint':r['review_fingerprint'],'request_key':KEY,'acknowledge_external_writes':True})
   self.assertEqual(ctx.exception.code,'stale_confirmation');self.assertFalse(any(x[0]=='POST' for x in f.calls))
 def test_moments_reviews_then_submits_single_fixed_task(self):
  f=Fake(True);c=Client(transport=f);r=c.call('wecom.moments-review',{'task_id':MID})
  out=c.call('wecom.moments-dispatch',{'task_id':MID,'review_fingerprint':r['review_fingerprint'],'request_key':KEY,'acknowledge_external_writes':True})
  posts=[x for x in f.calls if x[0]=='POST'];self.assertEqual(len(posts),2);self.assertTrue(posts[0][1].endswith('/review'));self.assertTrue(posts[1][1].endswith('/submit'));self.assertTrue(posts[1][2]['acknowledge_external_writes']);self.assertEqual(out['status'],'awaiting_member')
 def test_demo_or_already_published_refused(self):
  for change in [{'demo_only':True},{'status':'published'}]:
   f=Fake();f.row.update(change)
   with self.assertRaises(Error):Client(transport=f).call('publish.review',{'task_id':1})
 def test_default_jsonl_cannot_approve_even_explicit_allowlist(self):
  for cmd in ['publish.approve','wecom.moments-dispatch']:
   f=Fake();r=process(Client(transport=f),json.dumps({'id':'test','command':cmd,'args':{}}),{cmd})
   self.assertFalse(r['ok']);self.assertEqual(f.calls,[])
 def test_team_token_no_admin_fallback(self):
  f=Fake();f.token='cht_fixture';c=Client(transport=f)
  for cmd in ['publish.review','publish.approve','wecom.moments-review','wecom.moments-dispatch']:
   a={'task_id':MID if cmd.startswith('wecom.') else 1}
   if cmd in {'publish.approve','wecom.moments-dispatch'}:a.update(review_fingerprint='a'*64,request_key=KEY,acknowledge_external_writes=True)
   with self.assertRaises(Error) as ctx:c.call(cmd,a)
   self.assertEqual(ctx.exception.code,'team_scope')
  self.assertEqual(f.calls,[])
if __name__=='__main__':unittest.main()
