import json
import unittest
from creatorhub_cli.client import CATALOG, Client, Error, WRITE_COMMANDS


KEY = 'wecom-test-request-000001'


def record():
    return {'status':'completed','started_at':'2026-11-02T02:00:00+00:00',
        'finished_at':'2026-11-02T02:00:01+00:00',
        'counts':{'visible_member_count':1,'enabled_member_count':1,'customer_count':1}}


def snapshot():
    return {'protocol':'1.0','source':'existing_hub_summary_cache','cache_present':True,
        'cache_fresh':True,'cache_age_seconds':10,'clock_skew_detected':False,
        'automatic_polling_enabled':False,'platform_refresh_triggered':False,
        'raw_customer_ids_saved':False,'external_write_triggered':False,
        'last_success':record(),'last_attempt':record(),
        'private_ids':['PRIVATE_CUSTOMER'],'secret':'PRIVATE_SECRET'}


class Fake:
    def __init__(self, result=None):
        self.calls=[];self.result=result if result is not None else snapshot()
    def get(self,url):
        self.calls.append(('GET',url));return self.result
    def post(self,url,body,key):
        self.calls.append(('POST',url,body,key));return self.result


class WeComTests(unittest.TestCase):
    def test_three_commands_and_no_customer_details_or_grants(self):
        self.assertEqual([x for x in CATALOG if x.startswith('wecom.') and not x.startswith('wecom.moments-')],
                         ['wecom.status','wecom.request','wecom.refresh','wecom.business-status','wecom.business-request','wecom.business-refresh'])
        self.assertIn('wecom.refresh',WRITE_COMMANDS)

    def test_status_is_cached_get_only_and_projected(self):
        f=Fake();r=Client(transport=f).call('wecom.status')
        self.assertEqual(f.calls,[('GET','http://127.0.0.1:18080/api/integrations/wecom/status')])
        self.assertNotIn('PRIVATE',json.dumps(r))
        self.assertFalse(r['platform_refresh_triggered'])
        self.assertEqual(r['last_success']['counts']['customer_count'],1)

    def test_refresh_fixed_post_with_stable_key(self):
        value={**snapshot(),'refresh':record(),'platform_refresh_triggered':True}
        f=Fake(value);r=Client(transport=f).call('wecom.refresh',{'request_key':KEY,'acknowledge_customer_ids':True})
        self.assertEqual(f.calls,[('POST','http://127.0.0.1:18080/api/integrations/wecom/refresh',
                                  {'acknowledge_customer_ids':True},KEY)])
        self.assertTrue(r['platform_refresh_triggered'])

    def test_request_is_receipt_get_and_never_tencent(self):
        f=Fake({**snapshot(),'refresh':record(),'replayed':True})
        r=Client(transport=f).call('wecom.request',{'request_key':KEY})
        self.assertEqual(f.calls,[('GET','http://127.0.0.1:18080/api/integrations/wecom/requests/'+KEY)])
        self.assertTrue(r['replayed'])

    def test_false_or_missing_ack_no_post(self):
        for args in [{'request_key':KEY},{'request_key':KEY,'acknowledge_customer_ids':False},
                     {'request_key':KEY,'acknowledge_customer_ids':1}]:
            f=Fake()
            with self.assertRaises(Error):Client(transport=f).call('wecom.refresh',args)
            self.assertFalse(f.calls)

    def test_team_token_never_falls_back_to_local_owner(self):
        for command,args in [('wecom.status',{}),('wecom.request',{'request_key':KEY}),
                ('wecom.refresh',{'request_key':KEY,'acknowledge_customer_ids':True})]:
            f=Fake();f.token='cht_'+'a'*32
            with self.assertRaises(Error) as e:Client(transport=f).call(command,args)
            self.assertEqual(e.exception.code,'team_scope');self.assertFalse(f.calls)

    def test_no_caller_selected_routes_secret_user_or_endpoint(self):
        for args in [{'userid':'PRIVATE'}, {'credentials':'PRIVATE'}, {'endpoint':'https://example/'}]:
            f=Fake()
            with self.assertRaises(Error):Client(transport=f).call('wecom.status',args)
            self.assertFalse(f.calls)
        f=Fake();c=Client(transport=f)
        for path in ['/api/integrations/wecom/customers','/api/integrations/wecom/settings',
                     '/api/integrations/wecom/requests/../../secret']:
            with self.assertRaises(Error):c._get(path)
        for path in ['/api/integrations/wecom/send','/api/integrations/wecom/grant']:
            with self.assertRaises(Error):c._post(path,{},KEY)
        self.assertFalse(f.calls)

    def test_malformed_count_or_unsafe_server_contract_not_zero(self):
        bad=snapshot();bad['last_success']={**record(),'counts':{'visible_member_count':1,
            'enabled_member_count':1,'customer_count':True}}
        for value in [bad,{**snapshot(),'automatic_polling_enabled':True},
                      {**snapshot(),'raw_customer_ids_saved':True},{}]:
            with self.assertRaises(Error):Client(transport=Fake(value)).call('wecom.status')

    def test_failed_refresh_is_cli_error_but_receipt_remains_readable(self):
        value={**snapshot(),'refresh':{'status':'failed','api_code':60020}}
        with self.assertRaises(Error) as e:
            Client(transport=Fake(value)).call('wecom.refresh',{'request_key':KEY,'acknowledge_customer_ids':True})
        self.assertEqual(e.exception.code,'wecom_refresh_failed')
        r=Client(transport=Fake(value)).call('wecom.request',{'request_key':KEY})
        self.assertEqual(r['refresh']['api_code'],60020)

    def test_old_hub_feature_missing_is_not_fallback(self):
        class Missing(Fake):
            def get(self,url):
                self.calls.append(url);raise Error('hub_http_404','missing',5)
        f=Missing()
        with self.assertRaises(Error) as e:Client(transport=f).call('wecom.status')
        self.assertEqual(e.exception.code,'wecom_extension_unavailable')
        self.assertEqual(len(f.calls),1)


if __name__=='__main__':unittest.main()
