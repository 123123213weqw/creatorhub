from pathlib import Path
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
import urllib.error
import urllib.parse

from creatorhub_cli.client import CATALOG,Client,Error,Transport,configured_url,WRITE_COMMANDS


class Fake:
    def __init__(self,result=None,handler=None):self.result,self.handler,self.urls=result,handler,[]
    def get(self,url):
        self.urls.append(url)
        return self.handler(url) if self.handler else self.result


class ClientTests(unittest.TestCase):
    def code(self,expected,fn):
        with self.assertRaises(Error) as e:fn()
        self.assertEqual(e.exception.code,expected)

    def test_client_has_no_independent_state(self):
        c=Client();caps=c.call('capabilities')
        self.assertFalse(caps['independent_database']);self.assertFalse(caps['independent_queue']);self.assertFalse(caps['independent_approval'])
        self.assertFalse(hasattr(c,'store'));self.assertEqual(caps['write_commands'],sorted(WRITE_COMMANDS))

    def test_catalog_has_no_console_or_worker(self):
        self.assertEqual(len(CATALOG),83)
        self.assertFalse(any(w in CATALOG for w in ['serve','leads.approve','outbox','distribution','employee']))

    def test_boundary(self):
        for url in ['https://localhost:18080','http://evil:80','http://u:p@localhost:80','http://127.0.0.1:80/path','http://127.0.0.1:bad']:
            self.code('hub_boundary',lambda:Client(url))

    def test_routes_are_fixed_get_only(self):
        c=Client(transport=Fake())
        for route in ['/api/settings','/api/accounts/1/open-browser','https://evil','/api/../secret']:
            self.code('route_not_allowed',lambda:c._get(route))
        self.assertFalse(hasattr(c.transport,'request'));self.assertFalse(hasattr(c,'dispatch'))

    def test_accounts_sanitized_and_paginated(self):
        n=Fake([{'id':i,'platform':'douyin','nickname':'cookie=never-show','storage_state':'secret','proxy':'secret'} for i in range(25)])
        r=Client(transport=n).call('accounts.list',{'page':2,'page_size':10,'platform':'douyin'})
        self.assertEqual(len(r['items']),10);self.assertEqual(r['items'][0]['id'],10)
        self.assertNotIn('never-show',json.dumps(r));self.assertNotIn('storage_state',json.dumps(r))
        self.assertIn('/api/accounts?',n.urls[0]);self.assertFalse(r['platform_refresh_triggered'])

    def test_tasks_use_existing_hub_queue(self):
        n=Fake({'items':[{'id':2,'key':'publish:2','queue_type':'publish','state':'pending','cookie':'secret'}],'total':1})
        r=Client(transport=n).call('tasks.list',{'state':'all'})
        self.assertIn('/api/task-queue?',n.urls[0]);self.assertEqual(r['items'][0]['key'],'publish:2')
        self.assertNotIn('cookie',json.dumps(r));self.assertEqual(r['state_authority'],'existing_creatorhub')

    def test_unknown_args_and_false_integer(self):
        c=Client()
        for command,args,expected in [('accounts.list',{'actor':'admin'},'invalid_arguments'),('accounts.list',{'page':True},'invalid_integer'),('accounts.list',{'page_size':101},'invalid_integer'),('accounts.list',[],'invalid_arguments'),('serve',{},'unknown_command')]:
            self.code(expected,lambda:c.call(command,args))

    def test_wrong_work_account_denied(self):
        n=Fake([{'id':12,'account_id':1}]);c=Client(transport=n)
        self.code('work_account_mismatch',lambda:c.call('comments.list',{'account_id':3,'work_id':12}))
        self.assertFalse(any('/comments' in u for u in n.urls))

    def test_comment_snapshot_not_fresh_scrape(self):
        n=Fake(handler=lambda u:[{'id':12,'account_id':1,'platform':'douyin'}] if '/api/account-works?' in u else [{'id':20,'text':'想了解报价','comment_id':'c1','cookie':'secret'}])
        r=Client(transport=n).call('comments.list',{'account_id':1,'work_id':12})
        self.assertEqual(r['items'][0]['text'],'想了解报价');self.assertFalse(r['platform_refresh_triggered'])
        self.assertFalse(any('/sync' in u for u in n.urls))

    def test_collection_platform_mismatch(self):
        n=Fake({'platform':'xhs'})
        self.code('platform_mismatch',lambda:Client(transport=n).call('collections.results',{'job_id':1,'platform':'douyin'}))
        self.assertEqual(len(n.urls),1)

    def test_publish_read_only_no_result_secret(self):
        n=Fake([{'id':1,'status':'pending','media_json':'secret-path','result_url':'https://secret/?token=secret'}])
        r=Client(transport=n).call('publish.list')
        self.assertEqual(n.urls,['http://127.0.0.1:18080/api/publish']);self.assertNotIn('secret',json.dumps(r))

    def test_risk_not_cleared_or_probed(self):
        n=Fake({'total':2,'counts':{'normal':1,'verification_required':1,'secret':'hidden'},'abnormal':1})
        r=Client(transport=n).call('risk.status')
        self.assertEqual(r['counts']['verification_required'],1);self.assertNotIn('secret',json.dumps(r))
        self.assertEqual(n.urls,['http://127.0.0.1:18080/api/risk-control/summary'])

    def test_schema_matches_catalog(self):
        s=Client().call('schema')['commands'];self.assertEqual(set(s),set(CATALOG))
        self.assertTrue(all(not x['additionalProperties'] for x in s.values()))

    def test_doctor_is_health_get(self):
        n=Fake({'status':'ok'});r=Client(transport=n).call('doctor')
        self.assertTrue(r['hub_reachable']);self.assertFalse(r['new_service_started'])
        self.assertEqual(n.urls,['http://127.0.0.1:18080/health'])

    def test_missing_or_malformed_not_zero(self):
        self.code('invalid_response',lambda:Client(transport=Fake({})).call('accounts.list'))
        self.code('hub_unhealthy',lambda:Client(transport=Fake({'status':'failed'})).call('doctor'))

    def test_stats_unknown_preserved(self):
        r=Client(transport=Fake({'trend':[],'works':[{'id':1,'play_count':None}]})).call('stats.show',{'account_id':1})
        self.assertIsNone(r['works'][0]['play_count']);self.assertIn('caveat',r)

    def test_no_business_files_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ,{'HOME':tmp}):Client().call('schema')
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_config_rejects_credentials_and_world_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'config.json';p.write_text('{"hub_url":"http://127.0.0.1:18080"}');p.chmod(0o644)
            self.code('private_config',lambda:configured_url(p));p.chmod(0o600)
            self.assertEqual(configured_url(p),'http://127.0.0.1:18080')
            p.write_text('{"hub_url":"http://127.0.0.1:18080","secret":"do-not-use"}')
            self.code('invalid_config',lambda:configured_url(p))

    def test_transport_bounded_retry(self):
        opener=Mock();opener.open.side_effect=TimeoutError()
        with patch('urllib.request.build_opener',return_value=opener),patch('time.sleep'):
            self.code('hub_unavailable',lambda:Transport().get('http://127.0.0.1:18080/health'))
        self.assertEqual(opener.open.call_count,2)

    def test_transport_does_not_retry_forbidden(self):
        opener=Mock();opener.open.side_effect=urllib.error.HTTPError('http://127.0.0.1:18080',403,'denied',{},None)
        with patch('urllib.request.build_opener',return_value=opener):
            self.code('hub_http_403',lambda:Transport().get('http://127.0.0.1:18080/health'))
        self.assertEqual(opener.open.call_count,1)

    def test_cli_json_and_retired_commands(self):
        for command in [('serve',),('worker',),('publish','approve'),('distribution','draft')]:
            r=subprocess.run([sys.executable,'-m','creatorhub_cli.cli',*command],capture_output=True,text=True,timeout=5)
            self.assertEqual(r.returncode,2);self.assertFalse(json.loads(r.stdout)['ok']);self.assertEqual(r.stderr,'')
        r=subprocess.run([sys.executable,'-m','creatorhub_cli.cli','capabilities','--json'],capture_output=True,text=True,timeout=5)
        self.assertEqual(r.returncode,0);self.assertEqual(json.loads(r.stdout)['data']['interface'],'cli_only')


if __name__=='__main__':unittest.main()
