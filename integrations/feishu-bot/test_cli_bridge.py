import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cli_bridge import CLIBridge,HELP
from hub import HubError,TOOLS


class BridgeTest(unittest.TestCase):
    def make(self,runner=None):
        self.calls=[]
        def run(command,args):
            self.calls.append((command,args))
            if runner:return runner(command,args)
            if command=='assets.list':return {'ok':True,'data':{'items':[{'id':'asset_'+'a'*32,'label':'演示素材','is_demo':True}]}}
            return {'ok':True,'data':{'id':8,'status':'draft' if command=='publish.draft' else 'pending','demo_only':True}}
        bridge=CLIBridge({'base_url':'http://127.0.0.1:18080','label':'fixture'},['owner'],runner=run,secrets=['SECRET'])
        return bridge

    def test_read_tools_only_and_no_model_write(self):
        bridge=self.make()
        self.assertEqual(len(TOOLS),4)
        r=bridge.execute('hub_publish',{},actor='owner')
        self.assertFalse(r['ok']);self.assertEqual(self.calls,[])
        for tool,command in [('hub_accounts','accounts.list'),('hub_tasks','tasks.list')]:
            r=bridge.execute(tool,{},actor='owner');self.assertTrue(r['ok']);self.assertEqual(self.calls[-1][0],command)

    def test_foreign_identity_cannot_run_anything(self):
        bridge=self.make()
        self.assertFalse(bridge.execute('hub_accounts',{},actor='other')['ok'])
        handled,reply=bridge.handle_command('/同步作品 1','other','message-1')
        self.assertTrue(handled);self.assertEqual(self.calls,[])

    def test_sync_key_is_trusted_and_stable_across_delivery_restart(self):
        bridge=self.make()
        a=bridge.handle_command('/同步作品 1','owner','same-message')
        b=bridge.handle_command('/同步作品 1','owner','same-message')
        self.assertTrue(a[0] and b[0]);self.assertEqual(self.calls[0],self.calls[1])
        self.assertEqual(self.calls[0][0],'sync.request');self.assertEqual(self.calls[0][1]['account_id'],1)
        self.assertRegex(self.calls[0][1]['request_key'],r'^bot-sync-[a-f0-9]{48}$')
        with self.assertRaises(HubError):bridge.invoke('sync.request',{'account_id':1},'owner')

    def test_draft_only_asset_ids_never_paths_or_actor_or_approve(self):
        bridge=self.make()
        for args in [
            {'account_id':1,'media_type':'images','media_paths':['/private/key.jpg']},
            {'account_id':1,'media_type':'images','asset_ids':['asset_'+'a'*32],'actor':'owner'},
            {'account_id':1,'media_type':'images','asset_ids':['asset_'+'a'*32],'run_now':True},
            {'account_id':1,'media_type':'images','asset_ids':['asset_'+'a'*32],'request_key':'model-key'}]:
            bridge.handle_command('/草稿 '+json.dumps(args),'owner','msg')
        self.assertEqual(self.calls,[])
        args={'account_id':1,'media_type':'images','asset_ids':['asset_'+'a'*32],'title':'测试'}
        handled,reply=bridge.handle_command('/草稿 '+json.dumps(args),'owner','msg2')
        self.assertTrue(handled);self.assertEqual(self.calls[0][0],'publish.draft')
        self.assertIn('未发布',reply);self.assertIn('禁止真实发布',reply)

    def test_unknown_result_provides_receipt_lookup_no_post_retry(self):
        bridge=self.make(lambda c,a:{'ok':False,'error':{'code':'submission_unknown'}})
        handled,reply=bridge.handle_command('/同步作品 1','owner','msg')
        self.assertTrue(handled);self.assertIn('/核对 bot-sync-',reply);self.assertEqual(len(self.calls),1)
        key=self.calls[0][1]['request_key']
        bridge.handle_command('/核对 '+key,'owner','other-message')
        self.assertEqual(self.calls[-1],('requests.status',{'operation':'sync','request_key':key}))

    def test_crm_requires_human_review_even_for_demo(self):
        bridge=self.make(lambda c,a:{'ok':True,'data':{'id':1,'state':'candidate','version':1,'digest':'a'*64,'is_demo':True}})
        bridge.handle_command('/CRM演练 1','owner','message')
        self.assertEqual([c[0] for c in self.calls],['leads.status'])

    def test_crm_mock_uses_current_reviewed_version_not_user_supplied_fields(self):
        def runner(command,args):
            if command=='leads.status':return {'ok':True,'data':{'state':'approved','version':2,'digest':'a'*64}}
            return {'ok':True,'data':{'mock_record_id':4,'deduplicated':False}}
        bridge=self.make(runner)
        _,reply=bridge.handle_command('/CRM演练 1','owner','message')
        self.assertIn('未写真实飞书表',reply)
        self.assertEqual(self.calls[-1][0],'crm.mock');self.assertEqual(self.calls[-1][1]['version'],2)

    def test_raw_comments_are_not_echoed_to_feishu_or_cloud(self):
        bridge=self.make(lambda c,a:{'ok':True,'data':{'id':1,'state':'candidate','comment_text':'SECRET 13812345678','source_nickname':'SECRET','version':1}})
        _,reply=bridge.handle_command('/意向状态 1','owner','message')
        self.assertNotIn('SECRET',reply);self.assertNotIn('13812345678',reply)
        self.assertTrue(all(c[0]!='comments.list' for c in self.calls))

    def test_malformed_private_command_never_falls_back_to_model(self):
        bridge=self.make()
        for text in ['/草稿 invalid /private/secret','/同步作品 abc','/未知命令','/CRM演练 1 2','/publish approve 8']:
            handled,_=bridge.handle_command(text,'owner','m');self.assertTrue(handled)
        self.assertEqual(self.calls,[])
        self.assertFalse(bridge.handle_command('你好','owner','m')[0])

    def test_audit_excludes_arguments_and_comment_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'audit.jsonl'
            bridge=CLIBridge({'base_url':'http://127.0.0.1:18080'},['owner'],p,runner=lambda c,a:{'ok':True,'data':{'id':1,'status':'draft','demo_only':True}})
            bridge.handle_command('/草稿 '+json.dumps({'account_id':1,'media_type':'images','asset_ids':['asset_'+'a'*32],'desc':'PRIVATE_BODY'}),'owner','m')
            data=p.read_text();self.assertNotIn('PRIVATE_BODY',data);self.assertNotIn('asset_',data);self.assertNotIn('"owner"',data)
            for h in bridge.audit.handlers:h.close()


if __name__=='__main__':unittest.main()
