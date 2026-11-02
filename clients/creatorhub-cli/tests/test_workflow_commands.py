import unittest
from creatorhub_cli.client import Client,Error,CATALOG


class Fake:
    def __init__(self):self.calls=[]
    def get(self,url):
        self.calls.append(('GET',url))
        if '/crm-preview' in url:return {'mode':'local_mock','real_table_configured':False,'fields':{'来源唯一编号':'PRIVATE_EXTERNAL','咨询原文':'PRIVATE_CONTACT'}}
        if '/local-assets' in url:return [{'id':'asset_'+'a'*32,'label':'test','media_type':'images','is_demo':True,'manifest_json':'PRIVATE_PATH'}]
        if '/client-receipts/' in url:return {'id':1,'status':'draft','receipt_found':True}
        return {'id':1,'state':'candidate','version':1,'digest':'a'*64,'comment_text':'PRIVATE_CONTACT','source_nickname':'PRIVATE_NAME'}
    def post(self,url,body,key):
        self.calls.append(('POST',url,body,key))
        if url.endswith('/drafts'):return {'id':1,'status':'draft','review_required':True,'review_digest':'a'*64,'demo_only':True}
        return {'id':1,'mode':'local_mock','external_write_triggered':False,'state':'candidate','is_demo':True}


class WorkflowTest(unittest.TestCase):
    def test_incomplete_write_receipts_are_unknown_not_success(self):
        class Broken(Fake):
            def post(self,*args):return {}
        cases=[('assets.register',{'label':'demo','media_type':'images','media_paths':['/tmp/demo.png']}),
               ('leads.demo',{'account_id':1}),('leads.propose',{'account_id':1,'work_id':1}),
               ('crm.mock',{'lead_id':1,'version':1,'digest':'a'*64})]
        for command,args in cases:
            with self.subTest(command=command),self.assertRaises(Error) as error:
                Client(transport=Broken()).call(command,{**args,'request_key':'broken-receipt-0001'})
            self.assertEqual(error.exception.code,'submission_unknown')

    def test_doctor_distinguishes_v4_extensions_from_old_protocol(self):
        class Health(Fake):
            def get(self,*args):return {'status':'ok','hub_cli_protocol':'1.0','local_assets':True,'offline_leads':True,'crm_mode':'local_mock'}
        result=Client(transport=Health()).call('doctor')
        self.assertTrue(result['offline_leads_ready'] and result['local_assets_ready'])
        self.assertEqual(result['crm_mode'],'local_mock');self.assertFalse(result['real_crm_enabled'])

    def test_media_asset_ids_not_paths_can_create_draft(self):
        f=Fake();r=Client(transport=f).call('publish.draft',{'account_id':1,'media_type':'images','asset_ids':['asset_'+'a'*32],'request_key':'test-asset-draft-0001'})
        self.assertTrue(r['demo_only']);self.assertEqual(f.calls[0][1],'http://127.0.0.1:18080/api/publish/drafts')

    def test_two_media_sources_are_rejected(self):
        with self.assertRaises(Error):Client(transport=Fake()).call('publish.draft',{'account_id':1,'media_type':'images','asset_ids':['asset_'+'a'*32],'media_paths':['/tmp/a.jpg'],'request_key':'test-asset-draft-0001'})

    def test_bot_facing_lead_status_has_no_raw_private_comment(self):
        r=Client(transport=Fake()).call('leads.status',{'lead_id':1})
        self.assertNotIn('PRIVATE',str(r));self.assertNotIn('comment_text',r)

    def test_crm_preview_only_returns_field_names_not_contact_values(self):
        r=Client(transport=Fake()).call('crm.preview',{'lead_id':1})
        self.assertFalse(r['real_table_configured']);self.assertNotIn('PRIVATE',str(r));self.assertIn('咨询原文',r['field_names'])

    def test_asset_metadata_has_no_manifest_or_paths(self):
        r=Client(transport=Fake()).call('assets.list')
        self.assertNotIn('manifest_json',str(r));self.assertNotIn('PRIVATE_PATH',str(r))

    def test_receipt_lookup_is_read_only_and_path_bounded(self):
        f=Fake();r=Client(transport=f).call('requests.status',{'operation':'publish','request_key':'bot-publish-'+'a'*48})
        self.assertTrue(r['receipt_found']);self.assertEqual(f.calls[0][0],'GET')

    def test_crm_mock_cannot_select_real_destination_or_credentials(self):
        c=Client(transport=Fake())
        for extra in [{'table_id':'tblSomething'},{'access_token':'private'},{'mode':'real'}]:
            with self.assertRaises(Error):c.call('crm.mock',{'lead_id':1,'version':1,'digest':'a'*64,'request_key':'crm-test-000000001',**extra})
        self.assertNotIn('crm.write',CATALOG);self.assertNotIn('leads.approve',CATALOG)


if __name__=='__main__':unittest.main()
