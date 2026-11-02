import json
import unittest
from unittest.mock import Mock,patch
import urllib.error

from creatorhub_cli.client import Client,Error,Transport


class Fake:
    def __init__(self):self.calls=[]
    def post(self,url,body,key):
        self.calls.append(('POST',url,body,key))
        return {'id':7,'status':'draft' if '/drafts' in url else 'pending','review_required':True,'review_version':1,'review_digest':'a'*64,'storage_state':'never-show'}
    def get(self,url):
        self.calls.append(('GET',url))
        return {'id':7,'status':'draft','media_json':'private file path','error':''}


class WriteTests(unittest.TestCase):
    def code(self,code,fn):
        with self.assertRaises(Error) as e:fn()
        self.assertEqual(e.exception.code,code)

    def draft(self,**changes):
        return {'account_id':1,'request_key':'demo-draft-0000001','media_type':'images','media_paths':['/tmp/offline-fixture.jpg'],**changes}

    def test_draft_only_calls_non_executable_endpoint(self):
        t=Fake();r=Client(transport=t).call('publish.draft',self.draft(title='草稿'))
        self.assertEqual(t.calls[0][0:2],('POST','http://127.0.0.1:18080/api/publish/drafts'))
        self.assertNotIn('request_key',t.calls[0][2]);self.assertEqual(t.calls[0][3],'demo-draft-0000001')
        self.assertEqual(r['execution'],'human_confirmation_required_in_hub')
        self.assertNotIn('never-show',json.dumps(r));self.assertNotIn('media_paths',r)

    def test_sync_has_explicit_binding_and_existing_queue(self):
        t=Fake();c=Client(transport=t)
        c.call('sync.request',{'account_id':3,'kind':'comments','work_id':11,'request_key':'demo-comment-sync-01'})
        self.assertEqual(t.calls[0][1],'http://127.0.0.1:18080/api/sync-requests')
        self.assertEqual(t.calls[0][2],{'account_id':3,'kind':'comments','work_id':11})
        self.code('work_binding',lambda:c.call('sync.request',{'account_id':3,'kind':'comments','request_key':'demo-comment-sync-01'}))
        self.code('work_binding',lambda:c.call('sync.request',{'account_id':3,'kind':'works','work_id':11,'request_key':'demo-comment-sync-01'}))
        self.assertEqual(len(t.calls),1)

    def test_only_specific_post_routes(self):
        c=Client(transport=Fake())
        for path in ['/api/publish','/api/publish/7/approve','/api/publish/7/run-now','/api/settings','http://example.test']:
            self.code('route_not_allowed',lambda:c._post(path,{},'demo-key-00000001'))

    def test_status_never_triggers_platform(self):
        t=Fake();c=Client(transport=t)
        for command in ['publish.status','sync.status']:
            r=c.call(command,{'task_id':7});self.assertFalse(r['platform_refresh_triggered'])
        self.assertEqual(t.calls,[('GET','http://127.0.0.1:18080/api/publish/7'),('GET','http://127.0.0.1:18080/api/sync-requests/7')])
        self.assertNotIn('media_json',r)

    def test_no_approval_command_or_dangerous_parameters(self):
        c=Client(transport=Fake())
        for field in ['status','approved','auto_approve','run_now','cookie','storage_state']:
            self.code('invalid_arguments',lambda:c.call('publish.draft',self.draft(**{field:True})))
        self.code('invalid_arguments',lambda:c.call('publish.approve',{}))
        self.code('invalid_request_key',lambda:c.call('publish.draft',self.draft(request_key='short')))

    def test_limits_and_schedule(self):
        c=Client(transport=Fake())
        self.code('invalid_string',lambda:c.call('publish.draft',self.draft(title='x'*21)))
        self.code('invalid_boolean',lambda:c.call('publish.draft',self.draft(allow_save='true')))
        self.code('invalid_array',lambda:c.call('publish.draft',self.draft(media_paths=[])))
        self.code('invalid_media_path',lambda:c.call('publish.draft',self.draft(media_paths=['https://example.test/a.jpg'])))
        self.code('invalid_schedule',lambda:c.call('publish.draft',self.draft(scheduled_at='2026-12-01T10:00:00')))
        c.call('publish.draft',self.draft(scheduled_at='2026-12-01T10:00:00+08:00',allow_save=False))

    def test_timeout_unknown_and_no_post_retry(self):
        opener=Mock();opener.open.side_effect=TimeoutError()
        with patch('urllib.request.build_opener',return_value=opener):
            self.code('submission_unknown',lambda:Transport().post('http://127.0.0.1:18080/api/publish/drafts',{},'demo-key-00000001'))
        self.assertEqual(opener.open.call_count,1)

    def test_5xx_unknown_and_no_retry(self):
        for status in [500,502,503,504]:
            opener=Mock();opener.open.side_effect=urllib.error.HTTPError('http://127.0.0.1:18080',status,'SECRET',{},None)
            with patch('urllib.request.build_opener',return_value=opener):
                self.code('submission_unknown',lambda:Transport().post('http://127.0.0.1:18080/api/sync-requests',{},'demo-key-00000001'))
            self.assertEqual(opener.open.call_count,1)

    def test_4xx_and_redirect_not_followed(self):
        for status in [302,403,404,409,422,429]:
            opener=Mock();opener.open.side_effect=urllib.error.HTTPError('http://127.0.0.1:18080',status,'SECRET',{},None)
            with patch('urllib.request.build_opener',return_value=opener):
                self.code('hub_http_'+str(status),lambda:Transport().post('http://127.0.0.1:18080/api/sync-requests',{},'demo-key-00000001'))
            self.assertEqual(opener.open.call_count,1)

    def test_network_does_not_leak_payload_on_failure(self):
        opener=Mock();opener.open.side_effect=OSError('cookie=SECRET')
        with patch('urllib.request.build_opener',return_value=opener):
            try:Transport().post('http://127.0.0.1:18080/api/sync-requests',{'desc':'SECRET'},'demo-key-00000001')
            except Error as e:self.assertNotIn('SECRET',e.message)


if __name__=='__main__':unittest.main()
