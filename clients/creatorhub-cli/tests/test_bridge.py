import json,unittest
from creatorhub_cli.bridge import process
from creatorhub_cli.client import Client

class Fake:
    def __init__(self):self.calls=[]
    def call(self,command,args):self.calls.append((command,args));return {'status':'fixture'}

class BridgeTests(unittest.TestCase):
    def test_readonly_default(self):
        f=Fake();r=process(f,json.dumps({'id':'fixture','command':'accounts.list','args':{}}))
        self.assertTrue(r['ok']);self.assertEqual(len(f.calls),1)
        for command in ['publish.draft','sync.request','crm.preflight','wecom.business-refresh','backup.create','delivery.enqueue']:
            r=process(f,json.dumps({'id':'fixture','command':command,'args':{}}))
            self.assertFalse(r['ok']);self.assertEqual(r['error']['code'],'command_not_allowed')
        self.assertEqual(len(f.calls),1)
    def test_explicit_local_preparation_only(self):
        f=Fake()
        self.assertTrue(process(f,'{"id":"x","command":"publish.draft"}',{'publish.draft'})['ok'])
        self.assertFalse(process(f,'{"id":"x","command":"sync.request"}',{'sync.request'})['ok'])
    def test_untrusted_json_and_no_eval(self):
        f=Fake()
        for raw in ['[]','{"id":"x","id":"y","command":"schema"}',
                    '{"id":"x","command":"__import__(os)"}',
                    '{"id":"x","command":"schema","args":{"x":NaN}}',
                    '{"id":"x","command":"schema","shell":"rm"}']:
            self.assertFalse(process(f,raw)['ok'])
        self.assertEqual(f.calls,[])
    def test_policy_routes_and_contract(self):
        class Transport:
            token=''
            def get(self,url):
                self.url=url;return {'plan_only':True,'backup_due':None,'external_io_triggered':False,'root':'PRIVATE'}
        t=Transport();result=Client(transport=t).call('backup.policy')
        self.assertTrue(t.url.endswith('/api/operations/backup-policy'))
        self.assertIsNone(result['result']['backup_due']);self.assertNotIn('PRIVATE',json.dumps(result))

if __name__=='__main__':unittest.main()
