"""Offline technical coverage, not customer/employee/real CRM sign-off."""
import asyncio,io,json,hashlib
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock
import pytest
from PIL import Image
from sqlmodel import select
import app.main as main
import app.db as db
import app.delivery_workflows as delivery
import app.operations_health as health
from app.business_models import *
from app.models import DouyinAccount,PublishTask,LeadCandidate,LocalMediaAsset
from app.operations_workflows import digest as lead_digest
from test_project_optimizations import local_project,store
from test_access_and_submissions import client

@pytest.fixture
def project(local_project,monkeypatch):
    main.cfg.db_path=str(local_project.path)
    monkeypatch.setattr(main,'UPLOAD_DIR',local_project.root/'uploads')
    main.UPLOAD_DIR.mkdir()
    image=main.UPLOAD_DIR/'input.png';Image.new('RGB',(40,40),'blue').save(image)
    asset=LocalMediaAsset(id='asset_'+'c'*32,label='fixture',media_type='images',is_demo=True,manifest_json=json.dumps([{'path':str(image),'size':image.stat().st_size,'sha256':hashlib.sha256(image.read_bytes()).hexdigest(),'name':image.name}]))
    store(asset)
    aid=store(DouyinAccount(platform='douyin',nickname='fixture',status='active',storage_state='{}'))
    return local_project,asset.id,aid

async def post(http,path,body=None,token='',key=None,code=200):
    headers={'Idempotency-Key':key or 'business-test-'+__import__('uuid').uuid4().hex}
    if token:headers['Authorization']='Bearer '+token
    r=await http.post('/api/operations/'+path,json=body or {},headers=headers)
    assert r.status_code==code,r.text
    return r.json()

async def actor(http,role='employee',name='fixture'):
    a=await post(http,'actors',{'label':name,'roles':[role],'is_demo':True})
    c=await post(http,'actors/'+a['id']+'/credential',{'acknowledge_scope':True})
    return a['id'],c['token']

async def version(http,asset):
    c=await post(http,'contents',{'label':'fixture'})
    return await post(http,'contents/'+c['id']+'/versions',{'title':'Fixture not for publication','asset_ids':[asset]})

async def task(http,vid,employee,n=2):
    return await post(http,'distribution',{'version_id':vid,'employee_id':employee,'due_at':(datetime.utcnow()+timedelta(hours=1)).isoformat()+'Z',
        'targets':[{'channel':'manual','label':'target'+str(i)} for i in range(n)]})

async def proof(http,tid,revision,token,key='proof-business-idempotency-0001',color='red'):
    buf=io.BytesIO();Image.new('RGB',(50,50),color).save(buf,format='PNG')
    r=await http.post('/api/operations/targets/'+tid+'/proofs',files={'file':('proof.png',buf.getvalue(),'image/png')},
        data={'note':'Fixture send proof, not read or sale','revision':str(revision)},headers={'Authorization':'Bearer '+token,'Idempotency-Key':key})
    assert r.status_code==200,r.text
    return r.json()

async def get(http,path,token=''):
    return await http.get('/api/operations/'+path,headers={'Authorization':'Bearer '+token} if token else {})


def test_team_boundary_role_forgery_and_revocation(project):
    async def run():
        async with client() as h:
            aid,t=await actor(h)
            r=await get(h,'session',t);assert r.json()['id']==aid and not r.json()['local_owner']
            assert (await h.get('/api/accounts',headers={'Authorization':'Bearer '+t})).status_code==403
            assert (await h.get('/static/app.js',headers={'Authorization':'Bearer '+t})).status_code==403
            await post(h,'actors',{'label':'forged','roles':['admin']},t,code=403)
            assert (await h.get('/api/operations/actors',headers={'Authorization':'Bearer '+t,'X-CreatorHub-Actor':'local_owner'})).status_code==403
            await post(h,'actors/'+aid+'/state',{'enabled':False})
            assert (await get(h,'session',t)).status_code==401
            assert (await h.get('/api/operations/session',headers={'x-creatorhub-team-gateway':'1'})).status_code==401
    asyncio.run(run())


def test_expiry_and_no_raw_credential_in_db_or_audit(project):
    async def run():
        async with client() as h:
            aid,t=await actor(h)
            with db.get_session() as s:
                c=s.exec(select(TeamCredential)).first();c.expires_at=datetime.utcnow()-timedelta(seconds=1);s.add(c);s.commit()
                assert t not in json.dumps([r.model_dump(mode='json') for r in s.exec(select(BusinessAudit)).all()])
            assert (await get(h,'session',t)).status_code==401
    asyncio.run(run())


def test_version_variants_drafts_no_approval_and_material_tamper(project):
    _,asset,aid=project
    async def run():
        async with client() as h:
            v=await version(h,asset)
            a=await post(h,'versions/'+v['id']+'/variants',{'account_id':aid,'title':'fixture private'})
            p=await post(h,'plans',{'label':'fixture','variant_ids':[a['id']]})
            d=await post(h,'plans/'+p['id']+'/drafts');d2=await post(h,'plans/'+p['id']+'/drafts')
            assert d['items']==d2['items'] and not d['actual_published']
            with db.get_session() as s:
                rows=s.exec(select(PublishTask)).all();assert len(rows)==1 and rows[0].status=='draft' and rows[0].demo_only and rows[0].review_required
            (main.UPLOAD_DIR/'input.png').write_bytes(b'changed')
            await post(h,'versions/'+v['id']+'/variants',{'account_id':aid,'title':'tampered'},code=409)
    asyncio.run(run())


def test_target_scoped_access_partial_duplicate_review_revoke(project):
    _,asset,_=project
    async def run():
        async with client() as h:
            employee,t=await actor(h);other,ot=await actor(h,name='other');reviewer,rt=await actor(h,'reviewer');operator,op=await actor(h,'operator')
            v=await version(h,asset);d=await task(h,v['id'],employee);one,two=d['targets']
            assert (await get(h,'distribution/'+d['id'],ot)).status_code==403
            assert (await get(h,'distribution/'+d['id']+'/materials',t)).json()['version_digest']==v['digest']
            assert (await get(h,'distribution/'+d['id']+'/assets/'+asset+'/0',t)).status_code==200
            await post(h,'targets/'+one['id']+'/claim',{'revision':1},t)
            p=await proof(h,one['id'],2,t)
            assert (await proof(h,one['id'],2,t))['id']==p['id'] # retry old revision same receipt
            assert 'file_path' not in p
            assert (await get(h,'proofs/'+p['id']+'/file',ot)).status_code==403
            assert (await get(h,'proofs/'+p['id']+'/file',op)).status_code==403
            assert (await get(h,'proofs/'+p['id']+'/file',rt)).status_code==200
            args={'target_revision':3,'proof_sha256':p['sha256'],'decision':'accept','reason':'manual fixture review'}
            r=await post(h,'proofs/'+p['id']+'/review',args,rt);assert r['target']['status']=='verified' and not r['customer_read_verified']
            summary=(await get(h,'distribution/'+d['id'],t)).json();assert summary['completion']=='partial' and summary['verified_targets']==1
            p2=await proof(h,two['id'],1,t,key='proof-other-target-reused-0001');assert p2['duplicate_suspected']
            await post(h,'proofs/'+p2['id']+'/review',{**args,'target_revision':2,'proof_sha256':p2['sha256']},rt,code=409)
            await post(h,'proofs/'+p2['id']+'/review',{**args,'target_revision':2,'proof_sha256':p2['sha256'],'decision':'return'},rt)
            await post(h,'proofs/'+p['id']+'/review',{**args,'target_revision':4,'decision':'revoke'},rt)
            assert (await get(h,'distribution/'+d['id'],t)).json()['verified_targets']==0
            report=(await get(h,'reports',t)).json();assert report['customer_reads'] is None and report['sales'] is None
    asyncio.run(run())


def test_no_self_review_cancel_reassign_overdue_dedupe(project):
    _,asset,_=project
    async def run():
        async with client() as h:
            aid,t=await actor(h);bid,bt=await actor(h)
            # Deliberate dual-role fixture still cannot self-review.
            with db.get_session() as s:
                a=s.get(TeamActor,aid);a.roles_json='["employee","reviewer"]';s.add(a);s.commit()
            v=await version(h,asset);d=await task(h,v['id'],aid,1)
            await post(h,'distribution/'+d['id']+'/change',{'revision':1,'action':'reassign','employee_id':bid,'reason':'fixture transfer'})
            assert (await get(h,'distribution/'+d['id'],t)).status_code==200 # reviewer can see task, but not claim as employee
            await post(h,'targets/'+d['targets'][0]['id']+'/claim',{'revision':2},t,code=403)
            p=await proof(h,d['targets'][0]['id'],2,bt)
            with db.get_session() as s:
                b=s.get(TeamActor,bid);b.roles_json='["employee","reviewer"]';s.add(b)
                row=s.get(DistributionTask,d['id']);row.due_at=datetime.utcnow()-timedelta(hours=1);s.add(row);s.commit()
            await post(h,'proofs/'+p['id']+'/review',{'target_revision':3,'proof_sha256':p['sha256'],'decision':'accept','reason':'self'},bt,code=403)
            first=health.run_check();second=health.run_check();assert first['new_or_reopened']==1 and second['new_or_reopened']==0
            await post(h,'distribution/'+d['id']+'/change',{'revision':2,'action':'cancel','reason':'fixture cancel'})
            assert health.run_check()['resolved']==1
    asyncio.run(run())


def test_actor_scoped_idempotency_receipts(project):
    _,asset,_=project
    async def run():
        async with client() as h:
            a,ta=await actor(h);b,tb=await actor(h);v=await version(h,asset);d=await task(h,v['id'],a,1)
            await post(h,'targets/'+d['targets'][0]['id']+'/claim',{'revision':1},ta,key='shared-claim-key-00000001')
            await post(h,'targets/'+d['targets'][0]['id']+'/claim',{'revision':1},tb,key='shared-claim-key-00000001',code=403)
    asyncio.run(run())

async def lead_setup(h,mode='local_mock'):
    owner,ot=await actor(h,'sales','Sales fixture')
    row=LeadCandidate(source_key='fixture-source',external_key='fixture-person',platform='douyin',account_id=1,work_id=0,item_id='fixture',comment_id='fixture',source_nickname='fixture',comment_text='想了解费用',is_demo=True,owner_label='Sales fixture',state='approved',reviewed_at=datetime.utcnow())
    row.digest=lead_digest(row);row.approved_digest=row.digest;lid=store(row)
    await post(h,'delivery/config',{'mode':mode,'enabled':True})
    return owner,lid,row.digest


def test_outbox_record_notice_independent_and_disabled(project,monkeypatch):
    async def run():
        async with client() as h:
            assert not (await get(h,'delivery/config')).json()['enabled']
            owner,lid,hash_=await lead_setup(h)
            args={'lead_id':lid,'version':1,'digest':hash_,'owner_actor_id':owner}
            e=await post(h,'delivery/leads',args);again=await post(h,'delivery/leads',args)
            assert e['record']['id']==again['record']['id']
            await post(h,'delivery/'+e['notice']['id']+'/dispatch',code=409)
            r=await post(h,'delivery/'+e['record']['id']+'/dispatch');assert r['status']=='done'
            # Failure in separate notice never turns the record back to pending.
            with db.get_session() as s:
                n=s.get(DeliveryEvent,e['notice']['id']);n.status='failed';n.error_code='remote_rejected';s.add(n);s.commit()
            await post(h,'delivery/'+e['notice']['id']+'/resolve',{'action':'retry_confirmed_not_applied','reason':'test confirmed no delivery'})
            await post(h,'delivery/'+e['notice']['id']+'/dispatch')
            await post(h,'delivery/'+e['record']['id']+'/dispatch')
            with db.get_session() as s:
                assert len(s.exec(select(MockDeliveryRecord)).all())==2
                assert s.get(DeliveryEvent,e['record']['id']).attempts==1
            monkeypatch.setattr(delivery,'send_remote',Mock(side_effect=AssertionError('network forbidden')))
            await post(h,'delivery/config',{'mode':'unconfigured'})
            assert not (await get(h,'delivery/config')).json()['enabled']
    asyncio.run(run())


def test_unknown_recovery_never_reposts_and_mapping_changes_require_rebind(project):
    async def run():
        async with client() as h:
            owner,lid,hash_=await lead_setup(h)
            e=await post(h,'delivery/leads',{'lead_id':lid,'version':1,'digest':hash_,'owner_actor_id':owner})
            eid=e['record']['id']
            with db.get_session() as s:
                r=s.get(DeliveryEvent,eid);r.status='running';r.attempts=1;s.add(r);s.commit()
            assert delivery.recover()==1 and delivery.recover()==0
            await post(h,'delivery/'+eid+'/dispatch',code=409)
            await post(h,'delivery/'+eid+'/resolve',{'action':'confirm_applied','reason':'verified remote record','external_id':'rec-fixture'})
            assert (await post(h,'delivery/'+eid+'/dispatch'))['external_id']=='rec-fixture'
            await post(h,'delivery/config',{'mode':'local_mock','enabled':True,'mapping':{'bad':'mapping'}},code=422)
            await post(h,'delivery/config',{'mode':'local_mock','enabled':True,'owner_mapping':{owner:'ou_fixture123'}})
            await post(h,'delivery/'+e['notice']['id']+'/dispatch',code=409)
            await post(h,'delivery/'+e['notice']['id']+'/resolve',{'action':'bind','reason':'reviewed new destination'})
            assert (await post(h,'delivery/'+e['notice']['id']+'/dispatch'))['status']=='done'
    asyncio.run(run())


def test_manual_publish_review_is_separate_not_api_success(project):
    _,asset,aid=project
    async def run():
        async with client() as h:
            v=await version(h,asset);variant=await post(h,'versions/'+v['id']+'/variants',{'account_id':aid,'title':'private fixture'})
            args={'variant_id':variant['id'],'external_url':'https://www.douyin.com/video/fixture','published_at':datetime.utcnow().isoformat()+'Z','note':'manual isolated fixture'}
            m=await post(h,'manual-publishes',args);assert m['status']=='pending_review'
            await post(h,'manual-publishes/'+m['id']+'/review',{'decision':'verify','note':'self'},code=403)
            _,rt=await actor(h,'reviewer')
            r=await post(h,'manual-publishes/'+m['id']+'/review',{'decision':'verify','note':'fixture separate reviewer'},rt)
            assert r['status']=='verified'
            await post(h,'manual-publishes',{**args,'external_url':'https://evil.invalid/'},code=422)
    asyncio.run(run())


def test_portable_backup_includes_evidence_relations_but_no_credentials_or_grants(project):
    root,asset,_=project;folder=root.root
    main.cfg.engine.media_dir=str(folder/'media');(folder/'media').mkdir();(folder/'media/video.mp4').write_bytes(b'fixture video only')
    (folder/'target').mkdir();(folder/'restore').mkdir()
    async def backup(h,kind,body):
        r=await h.post('/api/video-backups/'+kind,json=body,headers={'Idempotency-Key':'business-backup-'+__import__('uuid').uuid4().hex})
        assert r.status_code==200,r.text
        if kind=='config':return r.json()
        row=(await h.get('/api/video-backups/'+r.json()['id'])).json();assert row['status']=='done',row
        return row
    async def run():
        async with client() as h:
            eid,t=await actor(h);v=await version(h,asset);d=await task(h,v['id'],eid,1);p=await proof(h,d['targets'][0]['id'],1,t)
            await backup(h,'config',{'provider':'local_directory','root':str(folder/'target')})
            snap=await backup(h,'snapshot',{})
            assert snap['result']['business_evidence_files']==2
            restored=await backup(h,'restore',{'snapshot_id':snap['id'],'restore_parent':str(folder/'restore')})
            target=Path(restored['result']['restore_path']);data=json.loads((target/'business.json').read_text())
            assert data['tables']['proofs'][0]['id']==p['id']
            assert data['tables']['proofs'][0]['target_id']==d['targets'][0]['id']
            assert len(list((target/'proofs').glob('*.png')))==1
            assert list((target/'business-assets').glob('*.png'))
            raw=(target/'business.json').read_text();assert t not in raw and 'teamcredential' not in data['tables'] and all('roles_json' not in a for a in data['tables']['actors'])
            assert not restored['result']['role_grants_applied'] and not restored['result']['external_events_replayed']
    asyncio.run(run())


def test_single_winner_claim_concurrent_revision(project):
    from concurrent.futures import ThreadPoolExecutor
    _,asset,_=project
    async def setup():
        async with client() as h:
            eid,t=await actor(h);v=await version(h,asset);d=await task(h,v['id'],eid,1);return t,d['targets'][0]['id']
    token,tid=asyncio.run(setup())
    def claim(i):
        async def run():
            async with client() as h:return await h.post('/api/operations/targets/'+tid+'/claim',json={'revision':1},headers={'Authorization':'Bearer '+token,'Idempotency-Key':'concurrent-claim-fixture-'+str(i)})
        return asyncio.run(run()).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(claim,[1,2]))==[200,409]


def test_real_transport_schema_types_and_timeout_are_fail_closed(project):
    import httpx
    from unittest.mock import patch
    from app.crm_adapter import DEFAULT_MAPPING
    cfg=CRMDeliveryConfig(mode='feishu',enabled=True,app_token='appfixture',table_id='tblfixture',mapping_json=json.dumps(DEFAULT_MAPPING))
    e=DeliveryEvent(id='delivery_'+'a'*32,dedupe_key='fixture',kind='crm_record',source_id='1',owner_actor_id='actor_fixture',payload_json=json.dumps({'fields':{k:'fixture' for k in DEFAULT_MAPPING}}),operation_token=str(__import__('uuid').uuid4()))
    class FakeHTTP:
        def __init__(self,wrong=False):self.wrong=wrong;self.posts=0
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,*a,**k):return httpx.Response(200,json={'code':0,'data':{'items':[{'field_name':v,'type':2 if self.wrong else 1} for v in DEFAULT_MAPPING.values()]}})
        def stream(self,*a,**k):
            from contextlib import contextmanager
            @contextmanager
            def response():yield self.get(*a,**k)
            return response()
        def post(self,*a,**k):self.posts+=1;raise httpx.ReadTimeout('lost reply')
    with patch.dict('os.environ',{'CREATORHUB_CRM_ACCESS_TOKEN':'fixture-access-token'},clear=False):
        f=FakeHTTP(True)
        with patch.object(delivery.httpx,'Client',return_value=f),pytest.raises(delivery.RemoteFailure,match='field_type_mismatch'):
            delivery.send_remote(cfg,e)
        assert f.posts==0
        f=FakeHTTP()
        with patch.object(delivery.httpx,'Client',return_value=f),pytest.raises(delivery.RemoteFailure,match='submission_unknown'):
            delivery.send_remote(cfg,e)
        assert f.posts==1


def test_content_metadata_and_variant_tamper_rejected(project):
    _,asset,aid=project
    async def run():
        async with client() as h:
            v=await version(h,asset)
            variant=await post(h,'versions/'+v['id']+'/variants',{'account_id':aid,'title':'fixture private'})
            with db.get_session() as s:
                r=s.get(ContentVariant,variant['id']);r.title='changed behind API';s.add(r);s.commit()
            await post(h,'plans',{'label':'tampered','variant_ids':[variant['id']]},code=409)
            with db.get_session() as s:
                r=s.get(ContentVersion,v['id']);r.description='changed behind API';s.add(r);s.commit()
            await post(h,'versions/'+v['id']+'/variants',{'account_id':aid,'title':'fixture'},code=409)
    asyncio.run(run())


def test_credential_lost_reply_does_not_issue_second_token(project):
    async def run():
        async with client() as h:
            a=await post(h,'actors',{'label':'fixture','roles':['employee']})
            body={'acknowledge_scope':True}
            await post(h,'actors/'+a['id']+'/credential',body,key='fixture-grant-single-0000001')
            await post(h,'actors/'+a['id']+'/credential',body,key='fixture-grant-single-0000001',code=409)
            with db.get_session() as s:assert len(s.exec(select(TeamCredential)).all())==1
    asyncio.run(run())
