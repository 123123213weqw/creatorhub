import copy,json,unittest,time
from creatorhub_cli.client import Client,Error,CATALOG,WRITE_COMMANDS


class Fake:
    token=''
    def __init__(self):self.calls=[];self.result={'protocol':'1.0','external_write_triggered':False,'raw_ids_saved':False,'automatic_polling_enabled':False}
    def get(self,url):self.calls.append(('GET',url));return self.result
    def post(self,url,body,key):self.calls.append(('POST',url,body,key));return self.result


class ExtensionTests(unittest.TestCase):
    def test_read_commands_only_hit_named_local_routes(self):
        f=Fake();c=Client(transport=f)
        for command,tail in [('crm.preflight-status','delivery/preflight'),('operations.profile','delivery-profile'),
                             ('operations.handoff','handoff'),('observations.list','observations')]:
            c.call(command);self.assertTrue(f.calls[-1][1].endswith('/api/operations/'+tail))
        c.call('wecom.business-status');self.assertTrue(f.calls[-1][1].endswith('/api/integrations/wecom/business/status'))
        self.assertTrue(all(x[0]=='GET' for x in f.calls))

    def test_wecom_ack_and_window_and_receipt(self):
        f=Fake();c=Client(transport=f);end=int(time.time())
        args={'request_key':'v9-cli-fixture-key-0001','start_time':end-10,'end_time':end,'acknowledge_business_records':False}
        with self.assertRaises(Error):c.call('wecom.business-refresh',args)
        self.assertEqual(f.calls,[])
        args['acknowledge_business_records']=True;c.call('wecom.business-refresh',args)
        self.assertEqual(f.calls[-1][2],{k:v for k,v in args.items() if k!='request_key'})
        c.call('wecom.business-request',{'request_key':args['request_key']});self.assertIn('/business/requests/',f.calls[-1][1])
        args['start_time']=end-8*86400
        with self.assertRaises(Error):c.call('wecom.business-refresh',args)

    def test_offline_nullable_schema_and_private_projection(self):
        f=Fake();c=Client(transport=f)
        f.result={'comments_json':'PRIVATE','text':'PRIVATE','external_userid':'PRIVATE','unknown_private_field':'PRIVATE',
                  'metrics':{'comments':{'value':None,'reason':'unavailable'}}}
        args={'request_key':'v9-cli-fixture-key-0001','account_id':1,'item_id':'fixture',
            'source_url':'https://www.douyin.com/video/fixture','source_kind':'manual_backend',
            'source_note':'fixture only','observed_at':'2026-10-30T12:00:00+08:00',
            'metrics':{k:{'value':None,'reason':'unavailable'} for k in ('views','likes','comments','shares','favorites')},
            'is_demo':True,'acknowledge_authorized_source':True}
        result=c.call('observations.import',args)
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertIsNone(result['result']['metrics']['comments']['value'])
        invalid=copy.deepcopy(args);invalid['metrics']['views']['value']=True
        with self.assertRaises(Error):c.call('observations.import',invalid)
        schema=c.call('schema')['commands']['observations.import']
        self.assertEqual(schema['properties']['metrics']['properties']['views']['properties']['value']['type'],['integer','null'])

    def test_no_admin_or_external_send_cli_commands(self):
        c=Client();f=Fake();c=Client(transport=f)
        for path in ['/api/operations/delivery-profile','/api/operations/delivery/config','/api/operations/delivery/delivery_'+'a'*32+'/dispatch']:
            with self.assertRaises(Error):c._post(path,{},'fixture-key-123456')
        self.assertIn('publish.approve',CATALOG);self.assertNotIn('wecom.send',CATALOG)
        self.assertIn('observations.import',WRITE_COMMANDS)
        f.token='cht_'+'a'*30
        with self.assertRaises(Error):c.call('wecom.business-status')
        self.assertEqual(f.calls,[])

    def test_preflight_never_enables_connector(self):
        f=Fake();c=Client(transport=f)
        c.call('crm.preflight',{'request_key':'v9-cli-preflight-key-0001'})
        self.assertEqual(f.calls[-1][2],{})
        self.assertTrue(f.calls[-1][1].endswith('/delivery/preflight'))


if __name__=='__main__':unittest.main()
