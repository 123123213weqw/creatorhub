"""Offline contracts for the model→CLI bridge. No real platform/model requests."""
from pathlib import Path
import hashlib,json,tempfile,unittest
from unittest.mock import patch
import httpx
from cli_bridge import CLIBridge,READ_MAP,ALLOWED_COMMANDS
from readonly_cli import TOOLS,NEW_MAP,validate,safe_metadata
from hub import HubError
from deepseek import DeepSeek

MID='wecom_moment_'+'a'*32
class ReadonlyTests(unittest.TestCase):
    def bridge(self,runner=None):
        self.calls=[]
        def run(cmd,args):
            self.calls.append((cmd,args))
            return runner(cmd,args) if runner else {'ok':True,'data':{'items':[{'id':MID,'status':'published','member_published':True,'text':'PRIVATE','sender_user_id':'PRIVATE'}],'external_write_triggered':False}}
        return CLIBridge({'base_url':'http://127.0.0.1:18080'},['owner'],runner=run,secrets=['PRIVATE'])
    def test_catalog_has_only_fixed_read_tools(self):
        self.assertEqual(len(TOOLS),23);self.assertEqual(len(NEW_MAP),19)
        for s in TOOLS:
            self.assertEqual(s['function']['parameters']['additionalProperties'],False)
        self.assertFalse(any(any(w in c for w in ['submit','review','dispatch','refresh','preflight','draft','create','upload','restore','register']) for c in NEW_MAP.values() if c!='crm.preflight-status'))
    def test_natural_model_tool_uses_installed_cli_route(self):
        b=self.bridge();r=b.execute('hub_cli_wecom_moments_status',{'task_id':MID},actor='owner')
        self.assertTrue(r['ok']);self.assertEqual(self.calls,[('wecom.moments-status',{'task_id':MID})])
        self.assertNotIn('PRIVATE',json.dumps(r));self.assertTrue(r['data']['items'][0]['member_published'])
    def test_schema_unknown_actor_urls_paths_and_bool_id_are_rejected(self):
        b=self.bridge()
        for name,args,actor in [
            ('hub_cli_wecom_moments_status',{'task_id':MID},'other'),
            ('hub_cli_wecom_moments_status',{'task_id':MID,'actor':'owner'},'owner'),
            ('hub_cli_wecom_moments_status',{},'owner'),
            ('hub_cli_publish_status',{'task_id':True},'owner'),
            ('hub_cli_publish_status',{'task_id':'../private/key'},'owner'),
            ('hub_cli_publish_list',{'url':'http://evil.invalid'},'owner'),
            ('hub_cli_publish_list',{'page_size':21},'owner'),
            ('hub_cli_wecom_moments_submit',{'task_id':MID},'owner'),
            ('shell',{'command':'rm -rf'},'owner')]:
            with self.subTest(name=name,args=args):self.assertFalse(b.execute(name,args,actor=actor)['ok'])
        self.assertEqual(self.calls,[])
    def test_unknown_invoke_command_denied_before_runner(self):
        b=self.bridge()
        for cmd in ['shell','publish.approve','delivery.dispatch','wecom.moments-refresh','wecom.moments-submit','backup.restore','/api/accounts','wecom.status;curl evil']:
            with self.assertRaises(HubError):b.invoke(cmd,{},'owner')
        self.assertEqual(self.calls,[])
    def test_capabilities_does_not_advertise_real_cli_writes(self):
        b=self.bridge(lambda c,a:{'ok':True,'data':{'version':'0.11.0','commands':['publish.approve'],'write_commands':['sync.request'],'console_url':'PRIVATE'}})
        r=b.execute('hub_cli_capabilities',{},actor='owner')
        self.assertTrue(r['ok']);self.assertEqual(r['data']['model_tool_count'],len(b.tools))
        self.assertEqual(r['data']['model_write_tools'],0)
        self.assertEqual(r['data']['model_write_preparations'],16);self.assertNotIn('PRIVATE',json.dumps(r))
    def test_projection_strips_sensitive_and_freeform_data_at_all_levels(self):
        raw={'items':[{'id':MID,'label':'PRIVATE','nickname':'PRIVATE','text':'PRIVATE','comment_text':'PRIVATE',
            'sender_user_id':'PRIVATE','customer_tag_ids':['PRIVATE'],'job_id':'PRIVATE',
            'moment_id':'PRIVATE','media':[{'path':'/private/key'}],'status':'published',
            'created_at':'2026-11-02T06:00:00Z','member_published':True}],
            'last_success':{'counts':{'visible_member_count':1,'customer_count':1},'token':'PRIVATE'},
            'status_counts':{'0':1,'1':1,'private':'PRIVATE'},'error':'PRIVATE',
            'read_count':None,'reply_count':None,'sales_count':None,'owner_mapping':{'ou_PRIVATE':'PRIVATE'}}
        r=safe_metadata(raw);self.assertNotIn('PRIVATE',json.dumps(r));self.assertNotIn('/private',json.dumps(r))
        self.assertEqual(r['status_counts'],{'0':1,'1':1});self.assertEqual(r['items'][0]['status'],'published')
        self.assertIsNone(r['read_count']);self.assertEqual(r['last_success']['counts']['customer_count'],1)
    def test_nulls_and_oversized_lists_are_not_successful_zero_or_complete(self):
        r=safe_metadata({'items':[{'id':i} for i in range(120)],'more_may_exist':False,'read_count':None})
        self.assertEqual(len(r['items']),100);self.assertTrue(r['projection_truncated']);self.assertTrue(r['more_may_exist']);self.assertIsNone(r['read_count'])
    def test_invalid_cli_response_errors_are_sanitized(self):
        for value in [{'ok':False,'error':{'code':'PRIVATE /private/key'}},{'ok':True,'data':[]},{'ok':True}]:
            b=self.bridge(lambda c,a:value);r=b.execute('hub_cli_wecom_moments_list',{},actor='owner')
            self.assertFalse(r['ok']);self.assertNotIn('PRIVATE',json.dumps(r));self.assertNotIn('/private',json.dumps(r))
    def test_audit_has_trusted_request_hash_but_no_inputs_or_results(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'audit.jsonl'
            b=CLIBridge({'base_url':'http://127.0.0.1:18080'},['owner'],path,runner=lambda c,a:{'ok':True,'data':{'status':'published','text':'PRIVATE'}})
            b.begin_message('test-message');b.execute('hub_cli_wecom_moments_status',{'task_id':MID},actor='owner')
            raw=path.read_text();r=json.loads(raw)
            self.assertEqual(r['request_hash'],hashlib.sha256(b'test-message').hexdigest()[:16]);self.assertNotIn(MID,raw);self.assertNotIn('PRIVATE',raw);self.assertNotIn('"owner"',raw)
            for h in b.audit.handlers:h.close()
    def test_current_launcher_can_follow_later_managed_release_without_old_pinning(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);link=root/'hubctl'
            for version in ['0.11.0','0.12.0']:
                path=root/version/'bin/hubctl';path.parent.mkdir(parents=True);path.write_text('#!/bin/sh\nprintf "'+version+'\\n"\n');path.chmod(0o700)
            link.symlink_to(root/'0.11.0/bin/hubctl')
            with patch('cli_bridge.CLI',link),patch('cli_bridge.CLI_RELEASE_ROOT',root):
                b=self.bridge();self.assertEqual(b._executable(),(root/'0.11.0/bin/hubctl').resolve());self.assertEqual(b.cli_version,'0.11.0')
                link.unlink();link.symlink_to(root/'0.12.0/bin/hubctl')
                self.assertEqual(b._executable(),(root/'0.12.0/bin/hubctl').resolve());self.assertEqual(b.cli_version,'0.12.0')
    def test_launcher_refuses_old_or_unmanaged_cli(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);path=root/'0.7.0/bin/hubctl';path.parent.mkdir(parents=True);path.write_text('#!/bin/sh\necho 0.7.0\n');path.chmod(0o700)
            with patch('cli_bridge.CLI',path),patch('cli_bridge.CLI_RELEASE_ROOT',root):
                b=self.bridge()
                with self.assertRaises(HubError):b._executable()
            with patch('cli_bridge.CLI_RELEASE_ROOT',root/'different'):
                b=self.bridge()
                with self.assertRaises(HubError):b._executable()
    def test_model_loop_receives_all_tools_and_projected_results(self):
        b=self.bridge();requests=[]
        def api(request):
            payload=json.loads(request.content);requests.append(payload)
            self.assertLessEqual(len(payload['tools']),16)
            if len(requests)==1:return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[{'id':'discover','function':{'name':'hub_tools_discover','arguments':'{"domain":"moments"}'}}]}}]})
            if len(requests)==2:return httpx.Response(200,json={'choices':[{'message':{'content':None,'tool_calls':[{'id':'fixture-1','type':'function','function':{'name':'hub_cli_wecom_moments_status','arguments':json.dumps({'task_id':MID})}}]}}]})
            self.assertEqual(payload['messages'][-1]['role'],'tool');self.assertNotIn('PRIVATE',payload['messages'][-1]['content'])
            return httpx.Response(200,json={'choices':[{'message':{'content':'任务已发表，但不代表客户已读。'}}]})
        with httpx.Client(transport=httpx.MockTransport(api)) as http:
            ai=DeepSeek({'api_key':'fixture','base_url':'https://api.deepseek.com','model':'deepseek-flash'},http=http,hub=b)
            r=ai.generate('fixture','实际查朋友圈任务状态',actor='owner')
        self.assertIn('已发表',r);self.assertIn('来源',r);self.assertEqual(ai.last_tool_names,['hub_tools_discover','hub_cli_wecom_moments_status']);self.assertEqual(len(self.calls),1)
    def test_model_cannot_convert_injected_results_into_write_permission(self):
        b=self.bridge();calls=[]
        def api(request):
            calls.append(1)
            if len(calls)==1:return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[{'id':'fixture-1','function':{'name':'hub_cli_wecom_moments_submit','arguments':json.dumps({'task_id':MID})}}]}}]})
            return httpx.Response(200,json={'choices':[{'message':{'content':'已经帮你派发'}}]})
        with httpx.Client(transport=httpx.MockTransport(api)) as http:
            ai=DeepSeek({'api_key':'fixture','base_url':'https://api.deepseek.com','model':'deepseek-flash'},http=http,hub=b)
            r=ai.generate('fixture','测试未开放的写接口',actor='owner')
        self.assertIn('查询未成功',r);self.assertNotIn('已经帮你派发',r);self.assertEqual(self.calls,[])

if __name__=='__main__':unittest.main()
