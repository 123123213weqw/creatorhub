"""All synthetic/offline. No permission changes, real sends or live publishing."""
import asyncio,json,uuid
from datetime import datetime,timedelta
from unittest.mock import Mock
import httpx,pytest
from sqlmodel import select
import app.db as db
import app.main as main
import app.crm_preflight as preflight
import app.delivery_workflows as delivery
import app.operations_health as health
from app.crm_adapter import DEFAULT_MAPPING,CRMError,map_fields,validate_contract
from app.business_models import CRMDeliveryConfig,DeliveryEvent,OpsIncident,TeamActor
from app.models import AppSetting,VideoBackupConfig,VideoBackupOperation
from app.delivery_profile import ProfileIn,KEY
from test_project_optimizations import local_project,store
from test_access_and_submissions import client

def payload():
    return {**{k:'fixture' for k in DEFAULT_MAPPING},'owner_label':'label-not-an-id','reviewed_at':'2026-11-02T11:00:00.123+08:00'}

def test_typed_owner_and_date_are_not_stringified():
    p=payload();types={'owner_label':'person','reviewed_at':'datetime'}
    result=map_fields(p,field_types=types,owner_open_id='ou_fixture_member')
    assert result['负责人标签']==[{'id':'ou_fixture_member'}]
    assert result['复核时间']==int(datetime.fromisoformat(p['reviewed_at']).timestamp()*1000)
    assert map_fields(p)['负责人标签']=='label-not-an-id'

@pytest.mark.parametrize('types',[{'account_id':'person'},{'reviewed_at':'number'},{'owner_label':'datetime'},{'unknown':'text'},[]])
def test_bad_typed_contract_rejected(types):
    with pytest.raises(CRMError):validate_contract(field_types=types)

@pytest.mark.parametrize('owner',[None,'someone','u_fixture','ou_x','ou_fixture\nTOKEN'])
def test_person_requires_explicit_app_openid(owner):
    with pytest.raises(CRMError,match='person_owner_binding_required'):
        map_fields(payload(),field_types={'owner_label':'person'},owner_open_id=owner)

@pytest.mark.parametrize('date',['2026-01-01T00:00:00','yesterday','1969-12-31T23:00:00Z',123,True,'2101-01-01T00:00:00Z'])
def test_dates_require_aware_bounded_iso(date):
    p=payload();p['reviewed_at']=date
    with pytest.raises(CRMError,match='timezone_required'):map_fields(p,field_types={'reviewed_at':'datetime'})

def test_typed_preflight_and_real_transport_use_same_contract(monkeypatch):
    monkeypatch.setenv('CREATORHUB_CRM_ACCESS_TOKEN','fixture-token')
    cfg=CRMDeliveryConfig(mode='feishu',enabled=True,app_token='appfixture',table_id='tblfixture',
        mapping_json=json.dumps(DEFAULT_MAPPING),field_types_json=json.dumps({'owner_label':'person','reviewed_at':'datetime'}),
        owner_mapping_json=json.dumps({'actor_fixture':'ou_fixture_member'}))
    event=DeliveryEvent(id='delivery_fixture',dedupe_key='fixture',kind='crm_record',source_id='1',owner_actor_id='actor_fixture',
        payload_json=json.dumps({'fields':payload()}),operation_token=str(uuid.uuid4()))
    calls=[]
    def respond(req):
        calls.append(req)
        if req.method=='GET':return httpx.Response(200,json={'code':0,'data':{'items':[
            {'field_name':name,'type':11 if key=='owner_label' else 5 if key=='reviewed_at' else 1}
            for key,name in DEFAULT_MAPPING.items()],'has_more':False}})
        body=json.loads(req.content);assert req.url.params['user_id_type']=='open_id'
        assert body['fields']['负责人标签']==[{'id':'ou_fixture_member'}]
        assert type(body['fields']['复核时间']) is int
        return httpx.Response(200,json={'code':0,'data':{'record':{'record_id':'rec_fixture'}}})
    factory=httpx.Client
    monkeypatch.setattr(delivery.httpx,'Client',lambda **kw:factory(transport=httpx.MockTransport(respond),**kw))
    assert delivery.send_remote(cfg,event)=='rec_fixture'
    assert [r.method for r in calls]==['GET','POST']

def test_backup_plan_keeps_unknowns_and_no_io(local_project,monkeypatch):
    monkeypatch.setattr('app.video_backup.make_store',Mock(side_effect=AssertionError('no provider access')))
    async def run():
        async with client() as h:
            r=(await h.get('/api/operations/backup-policy')).json()
            assert r['plan_only'] and r['backup_due'] is None
            assert not r['external_io_triggered'] and not r['retention_deletion_enabled']
            assert 'backup_destination_unconfigured' in r['blocked_reasons']
    asyncio.run(run())

def test_backup_plan_protects_two_latest_and_destination_binding(local_project):
    stamp=datetime.utcnow()
    store(VideoBackupConfig(provider='local_directory',root='/synthetic/not-opened',store_id='fixture-store'))
    with db.get_session() as s:
        s.add(AppSetting(key=KEY,value=ProfileIn(backup_interval_seconds=3600,backup_retention_days=1).model_dump_json()));s.commit()
    for index in range(4):
        store(VideoBackupOperation(id='vbak_'+str(index)*32,request_hash=str(index),payload_hash='fixture',kind='snapshot',status='done',
            provider='local_directory',root='/synthetic/not-opened',store_id='fixture-store',snapshot_id='snapshot'+str(index),
            manifest_sha256='a'*64,created_at=stamp-timedelta(days=index+2),finished_at=stamp-timedelta(days=index+2)))
    store(VideoBackupOperation(id='vbak_'+'f'*32,request_hash='other',payload_hash='fixture',kind='snapshot',status='done',
        provider='local_directory',root='/old-destination',store_id='other',snapshot_id='old',manifest_sha256='a'*64,finished_at=stamp))
    async def run():
        async with client() as h:
            r=(await h.get('/api/operations/backup-policy')).json()
            assert r['backup_due'] and r['successful_snapshots']==4
            assert r['protected_newest_snapshots']==2 and r['retention_candidate_count']==2
            assert not r['automatic_schedule_enabled']
    asyncio.run(run())
    with db.get_session() as s:assert len(s.exec(select(VideoBackupOperation)).all())==5

def test_alert_dedup_reopen_and_resolved_notice_blocked(local_project,monkeypatch):
    main.cfg.db_path=str(local_project.path)
    monkeypatch.setattr(health.shutil,'disk_usage',lambda _:type('D',(),{'free':1})())
    store(TeamActor(id='actor_fixture',label='fixture maintainer',roles_json='["maintainer"]',enabled=True))
    store(CRMDeliveryConfig(mode='local_mock',enabled=True,digest='fixture',alert_owner_actor_id='actor_fixture',owner_mapping_json='{"actor_fixture":"ou_fixture_member"}'))
    health.run_check();health.run_check()
    with db.get_session() as s:
        events=s.exec(select(DeliveryEvent).where(DeliveryEvent.kind=='incident_notice')).all()
        assert len(events)==1 and events[0].owner_actor_id=='actor_fixture';eid=events[0].id
    monkeypatch.setattr(health.shutil,'disk_usage',lambda _:type('D',(),{'free':10**12})())
    health.run_check()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as failure:delivery.dispatch(eid,'local_owner')
    assert failure.value.status_code==409
    monkeypatch.setattr(health.shutil,'disk_usage',lambda _:type('D',(),{'free':1})())
    health.run_check()
    with db.get_session() as s:
        events=s.exec(select(DeliveryEvent).where(DeliveryEvent.kind=='incident_notice')).all();assert len(events)==2
        latest=next(e for e in events if e.id!=eid)
    assert delivery.dispatch(latest.id,'local_owner')['status']=='done'
    with pytest.raises(HTTPException):delivery.dispatch(eid,'local_owner')

@pytest.mark.parametrize('destination,is_demo,owner_demo,allowed',[
    ('production',True,True,False),('production',False,False,True),
    ('sandbox',True,True,True),('sandbox',False,True,False),('sandbox',True,False,False)])
def test_dedicated_sandbox_never_accepts_real_customer_data(destination,is_demo,owner_demo,allowed):
    from fastapi import HTTPException
    cfg=CRMDeliveryConfig(destination_kind=destination)
    owner=TeamActor(id='actor_fixture',label='fixture',roles_json='["sales"]',is_demo=owner_demo)
    if allowed:delivery.check_destination_kind(cfg,is_demo,owner)
    else:
        with pytest.raises(HTTPException):delivery.check_destination_kind(cfg,is_demo,owner)


@pytest.mark.parametrize('platform,url',[
    ('douyin','https://www.douyin.com/video/fixture'),
    ('xhs','https://www.xiaohongshu.com/explore/fixture'),
    ('shipinhao','https://channels.weixin.qq.com/fixture'),
    ('kuaishou','https://www.kuaishou.com/short-video/fixture'),
    ('tiktok','https://www.tiktok.com/@fixture/video/fixture')])
def test_five_platform_offline_intent_provenance_not_live_collection(local_project,platform,url):
    from app.models import DouyinAccount,LeadCandidate
    from test_integrations_v9 import observation
    aid=store(DouyinAccount(platform=platform,nickname='fixture'));body=observation(aid)
    body['source_url']=url
    body['comments'] += [dict(body['comments'][0],comment_id='no-intent',text='不要联系我'),
                         dict(body['comments'][0],comment_id='like-only',text='好看，点赞')]
    async def run():
        async with client() as h:
            r=await h.post('/api/operations/observations',json=body,headers={'Idempotency-Key':'v10-five-platform-fixture'})
            assert r.status_code==200,r.text
            assert r.json()['leads_created']==1
            assert not r.json()['external_write_triggered']
    asyncio.run(run())
    with db.get_session() as s:
        rows=s.exec(select(LeadCandidate)).all();assert len(rows)==1
        assert rows[0].platform==platform and rows[0].is_demo


def test_sandbox_crm_http_closed_loop_typed_fields_and_independent_notice(local_project,monkeypatch):
    """Actual ASGI handlers + fake official transport; no real records/messages."""
    from app.models import DouyinAccount
    from test_integrations_v9 import observation
    from test_business_workflows import post,actor
    monkeypatch.setenv('CREATORHUB_CRM_ACCESS_TOKEN','fixture-token')
    account=store(DouyinAccount(platform='douyin',nickname='fixture'))
    calls=[];record_tokens=set();notices=0
    def respond(req):
        nonlocal notices
        calls.append((req.method,req.url.path))
        if req.method=='GET' and req.url.path.endswith('/fields'):
            return httpx.Response(200,json={'code':0,'data':{'items':[
                {'field_name':name,'type':11 if key=='owner_label' else 5 if key=='reviewed_at' else 1}
                for key,name in DEFAULT_MAPPING.items()],'has_more':False}})
        if req.method=='POST' and req.url.path.endswith('/records'):
            record_tokens.add(req.url.params['client_token'])
            fields=json.loads(req.content)['fields']
            assert fields['负责人标签']==[{'id':'ou_fixture_member'}] and type(fields['复核时间']) is int
            return httpx.Response(200,json={'code':0,'data':{'record':{'record_id':'rec_fixture'}}})
        if req.method=='POST' and req.url.path.endswith('/im/v1/messages'):
            notices+=1
            if notices==1:return httpx.Response(503,json={'code':503})
            return httpx.Response(200,json={'code':0,'data':{'message_id':'om_fixture'}})
        raise AssertionError('unapproved fixture endpoint')
    factory=httpx.Client
    monkeypatch.setattr(delivery.httpx,'Client',lambda **kw:factory(transport=httpx.MockTransport(respond),**kw))
    async def run():
        async with client() as h:
            owner,_=await actor(h,'sales','Sandbox test owner')
            settings={'mode':'feishu','enabled':True,'app_token':'appfixture','table_id':'tblfixture',
                'destination_kind':'sandbox','field_types':{'owner_label':'person','reviewed_at':'datetime'},
                'owner_mapping':{owner:'ou_fixture_member'},'acknowledge_external_writes':True}
            await post(h,'delivery/config',settings,code=422)
            settings['acknowledge_sandbox_destination']=True
            saved=await post(h,'delivery/config',settings);assert saved['destination_kind']=='sandbox'
            check=await post(h,'delivery/preflight');assert check['schema_ready'] and not check['write_permission_verified']
            imported=await post(h,'observations',observation(account))
            lead=(await h.get('/api/leads')).json()['items'][0]
            assert imported['leads_created']==1
            reviewed=await h.post('/api/leads/'+str(lead['id'])+'/review',json={
                'version':lead['version'],'digest':lead['digest'],'decision':'approve',
                'owner_label':'Sandbox test owner','note':'Synthetic fixture simulates manual review only'},
                headers={'Idempotency-Key':'v10-fixture-lead-human-review'})
            assert reviewed.status_code==200,reviewed.text
            request={'lead_id':lead['id'],'version':lead['version'],'digest':lead['digest'],'owner_actor_id':owner}
            events=await post(h,'delivery/leads',request)
            again=await post(h,'delivery/leads',request);assert again['record']['id']==events['record']['id']
            await post(h,'delivery/'+events['notice']['id']+'/dispatch',{'acknowledge_external_writes':True},code=409)
            record=await post(h,'delivery/'+events['record']['id']+'/dispatch',{'acknowledge_external_writes':True})
            assert record['status']=='done'
            failed=await post(h,'delivery/'+events['notice']['id']+'/dispatch',{'acknowledge_external_writes':True})
            assert failed['status']=='unknown'
            await post(h,'delivery/'+events['notice']['id']+'/dispatch',{'acknowledge_external_writes':True},code=409)
            await post(h,'delivery/'+events['notice']['id']+'/resolve',{
                'action':'retry_confirmed_not_applied','reason':'Fixture transport confirms first notice was not committed'})
            assert (await post(h,'delivery/'+events['notice']['id']+'/dispatch',{'acknowledge_external_writes':True}))['status']=='done'
            await post(h,'delivery/'+events['record']['id']+'/dispatch',{'acknowledge_external_writes':True})
    asyncio.run(run())
    assert len(record_tokens)==1 and notices==2
    assert sum(1 for method,path in calls if method=='POST' and path.endswith('/records'))==1
