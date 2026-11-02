"""Synthetic transports and isolated DB only. Never a customer acceptance claim."""
import asyncio,copy,json,time
from contextlib import contextmanager
from unittest.mock import Mock
import httpx,pytest
from sqlmodel import select
from app.models import DouyinAccount,LeadCandidate,AppSetting
from app.business_models import CRMDeliveryConfig,ObservationSnapshot
import app.db as db
import app.crm_preflight as crm
import app.wecom_business_workflows as flow
from app.wecom_business_readonly import WeComBusinessReadOnly,ROUTES
from app.wecom_readonly import Credentials,WeComError
from app.crm_adapter import DEFAULT_MAPPING,CRMError
from test_project_optimizations import local_project,store
from test_access_and_submissions import client

KEY='v9-fixture-request-00001'


def observation(aid=1):
    return {'account_id':aid,'item_id':'fixture-item','source_url':'https://www.douyin.com/video/fixture-item',
        'source_kind':'manual_backend','source_note':'Synthetic local rehearsal, not actual platform data',
        'observed_at':'2026-10-30T12:00:00+08:00',
        'metrics':{k:{'value':None,'reason':'not available in fixture'} for k in ('views','likes','comments','shares','favorites')},
        'comments':[{'comment_id':'fixture-comment','text':'想了解价格','nickname':'fixture',
                     'author_ref':'fixture-author','commented_at':'2026-10-30T11:00:00+08:00'}],
        'comments_complete':False,'is_demo':True,'acknowledge_authorized_source':True}


def test_offline_import_nulls_dedup_review_and_provenance(local_project):
    aid=store(DouyinAccount(platform='douyin',nickname='fixture'))
    body=observation(aid)
    async def run():
        async with client() as h:
            first=await h.post('/api/operations/observations',json=body,headers={'Idempotency-Key':KEY})
            assert first.status_code==200,first.text
            data=first.json();assert data['leads_created']==1
            assert data['snapshot']['metrics']['views']['value'] is None
            assert not data['external_write_triggered']
            replay=(await h.post('/api/operations/observations',json=body,headers={'Idempotency-Key':KEY})).json()
            assert replay.pop('replayed') is True and replay==data
            other=await h.post('/api/operations/observations',json=body,headers={'Idempotency-Key':KEY+'-other'})
            assert other.json()['created'] is False
            changed=copy.deepcopy(body);changed['comments'][0]['text']='不要联系我'
            assert (await h.post('/api/operations/observations',json=changed,headers={'Idempotency-Key':KEY})).status_code==409
            assert (await h.post('/api/operations/observations',json=changed,headers={'Idempotency-Key':KEY+'-same-time'})).status_code==409
            changed['observed_at']='2026-10-31T12:00:00+08:00'
            result=await h.post('/api/operations/observations',json=changed,headers={'Idempotency-Key':KEY+'-newer'})
            assert result.json()['leads_updated']==1
            historical=copy.deepcopy(body);historical['observed_at']='2026-10-29T12:00:00+08:00';historical['comments'][0]['commented_at']='2026-10-29T11:00:00+08:00'
            assert (await h.post('/api/operations/observations',json=historical,headers={'Idempotency-Key':KEY+'-older'})).status_code==200
            summary=(await h.get('/api/operations/observations')).json()
            assert '想了解' not in json.dumps(summary,ensure_ascii=False) and not summary['platform_refresh_triggered']
    asyncio.run(run())
    with db.get_session() as s:
        leads=s.exec(select(LeadCandidate)).all();assert len(leads)==1
        assert leads[0].state=='candidate' and leads[0].is_demo and leads[0].comment_text=='不要联系我'
        assert not leads[0].approved_digest and json.loads(leads[0].signal_tags)==[]


@pytest.mark.parametrize('mutation',[
    lambda b:b.update(acknowledge_authorized_source=False),
    lambda b:b['metrics']['views'].update(value=True),
    lambda b:b['metrics']['views'].update(value=None,reason=''),
    lambda b:b['metrics'].pop('likes'),
    lambda b:b.update(source_url='https://www.douyin.com@evil.invalid/'),
    lambda b:b.update(source_url='https://www.douyin.com/video/a?token=PRIVATE'),
    lambda b:b.update(source_url='http://127.0.0.1/private'),
    lambda b:b.update(observed_at='2099-01-01T00:00:00Z'),
    lambda b:b.update(observed_at='2026-01-01T12:00:00'),
    lambda b:b.update(comments=b['comments']*2),
    lambda b:b.update(work_id=999),
])
def test_offline_bad_source_rejected_without_partial_rows(local_project,mutation):
    aid=store(DouyinAccount(platform='douyin',nickname='fixture'));body=observation(aid);mutation(body)
    async def run():
        async with client() as h:
            r=await h.post('/api/operations/observations',json=body,headers={'Idempotency-Key':KEY})
            assert r.status_code in {409,422},r.text
    asyncio.run(run())
    with db.get_session() as s:assert not s.exec(select(ObservationSnapshot)).all()


def cfg():return CRMDeliveryConfig(mode='feishu',app_token='appfixture',table_id='tblfixture',mapping_json=json.dumps(DEFAULT_MAPPING),digest='fixture')


def test_crm_paginated_schema_is_readonly_and_type_strict(monkeypatch):
    monkeypatch.setenv('CREATORHUB_CRM_ACCESS_TOKEN','fixture-token')
    requests=[];names=list(DEFAULT_MAPPING.values())
    def handle(r):
        requests.append(r)
        assert r.method=='GET' and r.url.path.endswith('/fields')
        assert r.headers['authorization']=='Bearer fixture-token'
        second='page_token' in r.url.params
        return httpx.Response(200,json={'code':0,'data':{'items':[{'field_name':n,'type':1} for n in names[4:] if second] +
            [{'field_name':n,'type':1} for n in names[:4] if not second],
            'has_more':not second,'page_token':'second-page'}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as h:
        result=crm.inspect_schema(cfg(),h)
    assert result['schema_ready'] and len(requests)==2 and result['records_written']==0 and not result['write_permission_verified']


@pytest.mark.parametrize('body',[
    {'code':0,'data':{'items':[{'field_name':next(iter(DEFAULT_MAPPING.values())),'type':2}],'has_more':False}},
    {'code':0,'data':{'items':[],'has_more':False}},
])
def test_crm_schema_mismatch_not_ready(monkeypatch,body):
    monkeypatch.setenv('CREATORHUB_CRM_ACCESS_TOKEN','fixture-token')
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=body))) as h:
        result=crm.inspect_schema(cfg(),h)
    assert not result['schema_ready']


def test_crm_private_app_auth_requires_explicit_source(monkeypatch):
    monkeypatch.delenv('CREATORHUB_CRM_ACCESS_TOKEN',raising=False)
    import app.feishu_drive as fd
    fake=Mock(return_value=('fixture-renewed','fixture-app'));monkeypatch.setattr(fd,'token',fake)
    with pytest.raises(CRMError,match='credential_missing'):crm.access_token(None)
    fake.assert_not_called()
    assert crm.access_token(None,'local_feishu_app')=='fixture-renewed'
    with pytest.raises(CRMError):crm.access_token(None,'attacker-source')


def test_crm_schema_retry_bounded_no_post(monkeypatch):
    monkeypatch.setenv('CREATORHUB_CRM_ACCESS_TOKEN','fixture-token');monkeypatch.setattr(crm.time,'sleep',lambda _:None)
    calls=[]
    def fail(r):calls.append(r);raise httpx.ConnectError('PRIVATE_TOKEN',request=r)
    with httpx.Client(transport=httpx.MockTransport(fail)) as h,pytest.raises(CRMError,match='read_unavailable'):
        crm.inspect_schema(cfg(),h)
    assert len(calls)==2 and all(r.method=='GET' for r in calls)


def test_crm_http_preflight_copies_config_before_commit_and_replays(local_project,monkeypatch):
    calls=[]
    def inspect(c):
        assert c.mapping_json and c.app_token=='appfixture'  # must not be an expired ORM row
        calls.append(c.digest)
        return {'status':'ready','schema_ready':True,'config_digest':c.digest,'external_write_triggered':False}
    monkeypatch.setattr(crm,'check_config',inspect)
    store(cfg())
    async def run():
        async with client() as h:
            first=await h.post('/api/operations/delivery/preflight',json={},headers={'Idempotency-Key':KEY})
            assert first.status_code==200,first.text
            assert first.json()['schema_ready']
            second=await h.post('/api/operations/delivery/preflight',json={},headers={'Idempotency-Key':KEY})
            assert second.json()['replayed']
            assert (await h.post('/api/operations/delivery/preflight',json={},headers={'Idempotency-Key':KEY+'-new'})).status_code==429
    asyncio.run(run());assert len(calls)==1


def test_exclusion_and_backup_targets_are_not_acceptance(local_project):
    async def run():
        async with client() as h:
            p=(await h.get('/api/operations/delivery-profile')).json()['profile']
            p.update(employee_mobile_acceptance_required=False,employee_exclusion_reason='Owner excluded mobile employee acceptance on 2026-11-02',
                backup_rpo_seconds=3600,backup_rto_seconds=120,backup_retention_days=30)
            r=await h.post('/api/operations/delivery-profile',json=p,headers={'Idempotency-Key':KEY})
            assert r.status_code==200,r.text
            assert not r.json()['retention_deletion_enabled']
            facts=(await h.get('/api/operations/handoff')).json()
            assert facts['employee_mobile_acceptance']=='excluded_by_owner'
            assert not facts['customer_acceptance_complete'] and facts['pending']['real_crm_table_schema']
            readiness=(await h.get('/api/operations/readiness')).json()
            assert not readiness['employee_mobile_acceptance_required'] and not any('手机' in x for x in readiness['pending_external_inputs'])
    asyncio.run(run())
    from app.delivery_profile import drill_targets
    from datetime import datetime,timedelta
    result={};stamp=datetime.utcnow();drill_targets(result,stamp-timedelta(minutes=10),stamp,5)
    assert result['snapshot_age_target_met'] and result['isolated_restore_duration_target_met']
    assert not result['independent_backup_accepted'] and not result['customer_acceptance_complete']


def business_client(handler):
    client=WeComBusinessReadOnly(Credentials('ww1234567890',1000002,'fixture-secret'),transport=httpx.MockTransport(handler),sleep=lambda _:None)
    client._member=lambda:'fixture-member'
    client._access_token=lambda:'fixture-token'
    return client


def empty_handler(request):
    keys={'groupchat/list':'group_chat_list','get_groupmsg_list_v2':'group_msg_list','get_moment_list':'moment_list'}
    key=next(v for route,v in keys.items() if request.url.path.endswith(route))
    body=json.loads(request.content)
    if key=='group_chat_list':assert body['owner_filter']=={'userid_list':['fixture-member']}
    return httpx.Response(200,json={'errcode':0,key:[],'next_cursor':''})


def test_wecom_business_empty_is_api_verified_not_positive_sample():
    c=business_client(empty_handler);end=int(time.time())
    result=c.inspect(start_time=end-86400,end_time=end,acknowledge_business_records=True);c.close()
    assert result['status']=='ok' and result['business_post_requests']==4
    assert all(v['observed_count']==0 and v['api_read_verified'] and not v['positive_sample_verified'] for v in result['sections'].values())
    assert not result['raw_ids_saved'] and result['external_writes']==0


def test_wecom_business_positive_synthetic_and_privacy():
    def handler(request):
        body=json.loads(request.content);path=request.url.path
        if path.endswith('groupchat/list'):data={'group_chat_list':[{'chat_id':'PRIVATE_GROUP','status':0}]}
        elif path.endswith('get_groupmsg_list_v2'):data={'group_msg_list':[{'msgid':'PRIVATE_MESSAGE','create_type':0,'text':{'content':'PRIVATE_CONTENT'}}]}
        elif path.endswith('get_groupmsg_task'):data={'task_list':[{'userid':'fixture-member','status':2}]}
        elif path.endswith('get_groupmsg_send_result'):data={'send_list':[{'userid':'fixture-member','external_userid':'PRIVATE_CUSTOMER','chat_id':'PRIVATE_GROUP','status':1}]}
        elif path.endswith('get_moment_list'):data={'moment_list':[{'moment_id':'PRIVATE_MOMENT','create_type':0}]}
        elif path.endswith('get_moment_task'):data={'task_list':[{'userid':'fixture-member','publish_status':1}]}
        else:raise AssertionError(path)
        return httpx.Response(200,json={'errcode':0,'next_cursor':'',**data})
    c=business_client(handler);end=int(time.time());result=c.inspect(start_time=end-86400,end_time=end,acknowledge_business_records=True);c.close()
    assert result['status']=='ok' and 'PRIVATE' not in json.dumps(result)
    assert result['sections']['customer_messages']['recipient_status_counts']['1']==1
    assert result['sections']['moments']['member_published']==1
    assert result['sections']['customer_messages']['read_count'] is None


def test_wecom_business_scope_error_permission_error_and_no_write_routes():
    c=business_client(lambda r:httpx.Response(200,json={'errcode':48002,'errmsg':'PRIVATE error'}));end=int(time.time())
    result=c.inspect(start_time=end-1,end_time=end,acknowledge_business_records=True)
    assert all(x['observed_count'] is None for x in result['sections'].values())
    assert 'PRIVATE' not in json.dumps(result)
    with pytest.raises(WeComError,match='route_rejected'):c._read_post('/externalcontact/add_msg_template',{})
    with pytest.raises(WeComError):c.inspect(start_time=end-1,end_time=end)
    c.close()


def test_wecom_business_pagination_bound_is_not_complete():
    counter=[0]
    def handler(r):
        counter[0]+=1
        return httpx.Response(200,json={'errcode':0,'group_chat_list':[{'chat_id':str(counter[0]),'status':0}], 'next_cursor':str(counter[0])})
    c=business_client(handler);c._requests=0;c._deadline=time.monotonic()+20
    result=c._groups('fixture-member',1,2);c.close()
    assert not result['complete'] and result['observed_count']==3


def test_wecom_business_durable_receipt_payload_binding_and_global_pacing(local_project,monkeypatch):
    end=int(time.time());body=flow.RefreshIn(start_time=end-1,end_time=end,acknowledge_business_records=True)
    api=business_client(empty_handler);fake=Mock(return_value=api);monkeypatch.setattr(flow,'factory',fake)
    first=flow.refresh(KEY,body)
    assert first['refresh']['status']=='completed'
    assert flow.refresh(KEY,body)['replayed']
    with pytest.raises(Exception) as e:flow.refresh(KEY,body.model_copy(update={'start_time':end-2}))
    assert e.value.status_code==409
    with pytest.raises(Exception) as e:flow.refresh(KEY+'-other',body)
    assert e.value.status_code==429
    from app.wecom_workflows import refresh as old_refresh
    with pytest.raises(Exception) as e:old_refresh(KEY+'-customer-probe')
    assert e.value.status_code==429
    assert fake.call_count==1 and first['last_complete']['summary']['read_count'] is None


def test_new_sensitive_routes_deny_remote_and_team_headers(local_project):
    async def run():
        async with client(peer='192.0.2.1') as h:
            assert (await h.get('/api/operations/handoff')).status_code==403
        async with client() as h:
            for path in ['/api/integrations/wecom/business/status','/api/operations/delivery/preflight']:
                r=await h.get(path,headers={'Authorization':'Bearer cht_'+'a'*30})
                assert r.status_code in {401,403}
    asyncio.run(run())
