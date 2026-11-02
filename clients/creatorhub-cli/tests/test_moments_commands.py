import json,unittest
from creatorhub_cli.client import CATALOG,Client,Error,WRITE_COMMANDS
from creatorhub_cli.bridge import process

ID='wecom_moment_'+'a'*32
KEY='wecom-moment-fixture-key'
class Fake:
    token=''
    def __init__(self,value=None):self.calls=[];self.value=value or {'status':'draft','external_write_triggered':False}
    def get(self,url):self.calls.append(('GET',url));return self.value
    def post(self,url,body,key):self.calls.append(('POST',url,body,key));return self.value

class MomentsTests(unittest.TestCase):
    def test_catalog_and_version_no_submit_or_approval(self):
        c=Client();self.assertEqual(c.call('capabilities')['version'],'0.12.0')
        self.assertEqual(len([x for x in CATALOG if x.startswith('wecom.moments-')]),10)
        self.assertFalse(any(x in CATALOG for x in ['wecom.moments-submit','wecom.moments-cancel']))
        self.assertIn('wecom.moments-draft',WRITE_COMMANDS)
    def test_cached_routes_and_detail_projection(self):
        f=Fake({'id':ID,'status':'awaiting_member','member_published':None,'external_write_triggered':False,
                'text':'PRIVATE','sender_user_id':'PRIVATE','customer_tag_ids':['PRIVATE'],
                'job_id':'PRIVATE','moment_id':'PRIVATE','media':[{'path':'PRIVATE'}]})
        c=Client(transport=f)
        for cmd,args,suffix in [('wecom.moments-binding',{},'/binding'),
            ('wecom.moments-status',{'task_id':ID},'/'+ID),
            ('wecom.moments-receipt',{'operation':'preflight','request_key':KEY},'/receipts/preflight/'+KEY)]:
            result=c.call(cmd,args);self.assertNotIn('PRIVATE',json.dumps(result));self.assertTrue(f.calls[-1][1].endswith(suffix))
    def test_draft_exact_body_local_only_and_typed(self):
        f=Fake();args={'request_key':KEY,'label':'fixture','binding_id':'b'*64,
          'text':'offline fixture','customer_tag_ids':['etFIXTURE'],'customer_scope_note':'controlled test customers'}
        Client(transport=f).call('wecom.moments-draft',args)
        self.assertTrue(f.calls[0][1].endswith('/moments/drafts'));self.assertEqual(f.calls[0][3],KEY)
        self.assertNotIn('request_key',f.calls[0][2]);self.assertNotIn('sender_list',f.calls[0][2])
    def test_invalid_scope_and_content_do_not_post(self):
        base={'request_key':KEY,'label':'fixture','binding_id':'b'*64,'text':'fixture',
              'customer_tag_ids':['etFIXTURE'],'customer_scope_note':'controlled'}
        for change in [{'customer_tag_ids':[]},{'customer_tag_ids':['all']},
                       {'customer_tag_ids':['etABC','etABC']},{'text':'汉'*1500},
                       {'text':''},{'asset_ids':['asset_'+'c'*32]},
                       {'media_type':'link'},{'sender_list':{'department_list':[1]}},
                       {'customer_scope_note':' '}]:
            f=Fake()
            with self.assertRaises(Error):Client(transport=f).call('wecom.moments-draft',{**base,**change})
            self.assertEqual(f.calls,[])
    def test_read_ack_and_fixed_post(self):
        f=Fake();c=Client(transport=f)
        with self.assertRaises(Error):c.call('wecom.moments-refresh',{'task_id':ID,'request_key':KEY,'acknowledge_platform_read':False})
        self.assertEqual(f.calls,[])
        c.call('wecom.moments-refresh',{'task_id':ID,'request_key':KEY,'acknowledge_platform_read':True})
        self.assertEqual(f.calls[0][2],{'acknowledge_platform_read':True})
        self.assertTrue(f.calls[0][1].endswith('/'+ID+'/refresh'))
    def test_dangerous_routes_and_team_token_refused(self):
        f=Fake();c=Client(transport=f)
        for tail in ['/submit','/review','/cancel','/refresh?retry=1','/media/0','/../submit']:
            with self.assertRaises(Error):c._post('/api/integrations/wecom/moments/'+ID+tail,{},KEY)
        f.token='cht_fixture'
        with self.assertRaises(Error) as ctx:c.call('wecom.moments-status',{'task_id':ID})
        self.assertEqual(ctx.exception.code,'team_scope');self.assertEqual(f.calls,[])
    def test_list_projects_all_rows(self):
        f=Fake({'items':[{'id':ID,'external_write_triggered':False,'text':'PRIVATE'}],
                'external_write_triggered':False,'more_may_exist':False})
        r=Client(transport=f).call('wecom.moments-list');self.assertNotIn('PRIVATE',json.dumps(r))
        f.value['items'][0]['external_write_triggered']=True
        with self.assertRaises(Error):Client(transport=f).call('wecom.moments-list')
    def test_jsonl_defaults_and_prepare_allow(self):
        f=Fake();c=Client(transport=f)
        for cmd in ['wecom.moments-draft','wecom.moments-preflight','wecom.moments-refresh']:
            r=process(c,json.dumps({'id':'x','command':cmd,'args':{}}))
            self.assertEqual(r['error']['code'],'command_not_allowed')
        self.assertEqual(f.calls,[])
        for cmd in ['wecom.moments-preflight','wecom.moments-refresh']:
            r=process(c,json.dumps({'id':'x','command':cmd,'args':{}}),{cmd})
            self.assertEqual(r['error']['code'],'command_not_allowed')
        r=process(c,json.dumps({'id':'x','command':'wecom.moments-draft','args':{'request_key':KEY,'label':'fixture','binding_id':'b'*64,'text':'fixture','customer_tag_ids':['etFIXTURE'],'customer_scope_note':'controlled'}}),{'wecom.moments-draft'})
        self.assertTrue(r['ok']);self.assertEqual(len(f.calls),1)

if __name__=='__main__':unittest.main()
