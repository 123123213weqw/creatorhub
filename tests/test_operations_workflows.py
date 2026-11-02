"""Local-only material, lead review and CRM rehearsal acceptance tests."""
import asyncio
import json
import uuid
from unittest.mock import Mock

import httpx
import pytest
from sqlmodel import select

import app.db as db
import app.main as main
from app.models import (AccountWork,CommentRecord,DouyinAccount,LeadCandidate,MockCustomerRecord,PublishTask)
from app.operations_workflows import DEFAULT_MAPPING
from app.crm_adapter import FeishuBitableAdapter,CRMError,map_fields
from test_project_optimizations import local_project,store
from test_access_and_submissions import client


@pytest.fixture
def fixture(local_project,monkeypatch):
    account=store(DouyinAccount(platform='douyin',nickname='fixture',storage_state='{}'))
    work=store(AccountWork(platform='douyin',account_id=account,item_id='fixture-item'))
    monkeypatch.setattr(main,'UPLOAD_DIR',local_project.root/'uploads')
    media=local_project.root/'fixture.jpg';media.write_bytes(b'only an offline fixture')
    return account,work,media


def post(http,path,body,key='workflow-test-0001'):
    return http.post(path,json=body,headers={'Idempotency-Key':key})


def test_asset_id_draft_propagates_demo_and_cannot_publish(local_project,fixture):
    account,_,media=fixture
    async def run():
        async with client() as http:
            raw=await post(http,'/api/local-assets',{'label':'演示素材','media_type':'images','media_paths':[str(media)],'is_demo':True})
            assert raw.status_code==200,raw.text
            asset=raw.json();assert 'path' not in json.dumps(asset)
            assert (await post(http,'/api/local-assets',{'label':'演示素材','media_type':'images','media_paths':[str(media)],'is_demo':True})).json()['replayed']
            draft=await post(http,'/api/publish/drafts',{'account_id':account,'media_type':'images','asset_ids':[asset['id']]},'fixture-demo-draft01')
            assert draft.status_code==200,draft.text
            row=draft.json();assert row['demo_only'] and row['status']=='draft'
            approval={k:row[k] for k in ('review_version','review_digest')}
            assert (await post(http,f'/api/publish/{row["id"]}/approve',approval,'fixture-demo-approve')).status_code==422
            from app.engine.monitor import MonitorEngine
            with db.get_session() as s:
                task=s.get(PublishTask,row['id']);task.status='pending';s.add(task);s.commit()
            engine=MonitorEngine(local_project.cfg,local_project.browser)
            result=await engine._publish_task_locked(row['id'])
            assert not result['ok']
    asyncio.run(run())


def test_leads_offline_explainable_dedup_and_negative_filter(local_project,fixture):
    account,work,_=fixture
    for cid,text in [('a','想了解价格，怎么报名？'),('b','不需要，不要联系'),('c','拍得很好'),('d','价格多少')]:
        store(CommentRecord(platform='douyin',watch_id=0,aweme_id='fixture-item',comment_id=cid,text=text,user_sec_uid='user-'+cid))
    # A watched third-party comment on the same item must not be consumed.
    store(CommentRecord(platform='douyin',watch_id=22,aweme_id='fixture-item',comment_id='other',text='怎么报名'))
    async def run():
        async with client() as http:
            body={'account_id':account,'work_id':work}
            first=await post(http,'/api/leads/propose',body)
            assert first.status_code==200,first.text
            assert first.json()['created']==2 and first.json()['matched']==2
            assert not first.json()['platform_refresh_triggered']
            again=await post(http,'/api/leads/propose',body,'workflow-scan-again01')
            assert again.json()['created']==0
            rows=(await http.get('/api/leads')).json()['items'];assert len(rows)==2
            assert 'comment_text' not in rows[0]
    asyncio.run(run())


def test_candidate_review_then_local_mock_only_and_stable_dedupe(local_project,fixture):
    account,work,_=fixture
    store(CommentRecord(platform='douyin',watch_id=0,aweme_id='fixture-item',comment_id='a',text='想了解费用',user_sec_uid='same-user'))
    async def run():
        async with client() as http:
            await post(http,'/api/leads/propose',{'account_id':account,'work_id':work})
            row=(await http.get('/api/leads')).json()['items'][0];lid=row['id']
            version={k:row[k] for k in ('version','digest')}
            assert (await post(http,f'/api/leads/{lid}/crm-mock',version,'fixture-mock-before01')).status_code==409
            review={**version,'decision':'approve','owner_label':'演示负责人','note':'仅本机验证'}
            r=await post(http,f'/api/leads/{lid}/review',review,'fixture-review-00001')
            assert r.status_code==200,r.text
            assert (await post(http,f'/api/leads/{lid}/review',review,'fixture-review-00001')).json()['replayed']
            preview=(await http.get(f'/api/leads/{lid}/crm-preview')).json()
            assert preview['mode']=='local_mock' and not preview['real_table_configured']
            assert set(preview['fields'])==set(DEFAULT_MAPPING.values())
            result=await post(http,f'/api/leads/{lid}/crm-mock',version,'fixture-mock-000001')
            assert result.status_code==200,result.text
            assert result.json()['created'] and not result.json()['external_write_triggered']
            repeated=await post(http,f'/api/leads/{lid}/crm-mock',version,'fixture-mock-000002')
            assert repeated.json()['deduplicated']
            with db.get_session() as s:assert len(s.exec(select(MockCustomerRecord)).all())==1
    asyncio.run(run())


def test_changed_comment_revokes_review_and_wrong_account_rejected(local_project,fixture):
    account,work,_=fixture
    cid=store(CommentRecord(platform='douyin',watch_id=0,aweme_id='fixture-item',comment_id='a',text='想了解费用'))
    other=store(DouyinAccount(platform='douyin'))
    async def run():
        async with client() as http:
            assert (await post(http,'/api/leads/propose',{'account_id':other,'work_id':work})).status_code==422
            await post(http,'/api/leads/propose',{'account_id':account,'work_id':work})
            row=(await http.get('/api/leads')).json()['items'][0];lid=row['id']
            old={k:row[k] for k in ('version','digest')}
            await post(http,f'/api/leads/{lid}/review',{**old,'decision':'approve','owner_label':'owner','note':'reviewed'},'fixture-review-000001')
            with db.get_session() as s:
                c=s.get(CommentRecord,cid);c.text='想了解新的报名费用';s.add(c);s.commit()
            await post(http,'/api/leads/propose',{'account_id':account,'work_id':work},'fixture-new-scan001')
            changed=(await http.get(f'/api/leads/{lid}')).json()
            assert changed['state']=='candidate' and changed['version']==2 and changed['digest']!=old['digest']
            assert (await post(http,f'/api/leads/{lid}/crm-mock',old,'fixture-stale-mock01')).status_code==409
    asyncio.run(run())


def test_demo_candidate_is_explicit_and_does_not_fabricate_platform_comments(local_project,fixture):
    account,_,_=fixture
    async def run():
        async with client() as http:
            r=await post(http,'/api/leads/demo',{'account_id':account})
            assert r.status_code==200,r.text
            assert r.json()['is_demo']
            with db.get_session() as s:assert not s.exec(select(CommentRecord)).all()
    asyncio.run(run())


def test_receipt_lookup_reports_current_state_and_unknown_not_zero(local_project,fixture):
    account,_,media=fixture
    async def run():
        async with client() as http:
            row=(await post(http,'/api/publish/drafts',{'account_id':account,'media_type':'images','media_paths':[str(media)]})).json()
            receipt=(await http.get('/api/client-receipts/publish/workflow-test-0001')).json()
            assert receipt['id']==row['id'] and receipt['status']=='draft'
            assert 'desc' not in receipt
            assert (await http.get('/api/client-receipts/sync/nonexistent-key-0001')).status_code==404
    asyncio.run(run())


def test_real_crm_adapter_disabled_no_credentials_or_network():
    provider=Mock(side_effect=AssertionError('must not load credentials'))
    adapter=FeishuBitableAdapter('appMock001','tblMock001',provider)
    with pytest.raises(CRMError,match='real_table_not_enabled'):adapter.create({'f':'x'},str(uuid.uuid4()))
    provider.assert_not_called()


def test_crm_http_uses_stable_uuid_no_retry_and_type_contract():
    calls=[]
    def handler(r):
        calls.append(r);return httpx.Response(200,json={'code':0,'data':{'record':{'record_id':'recFixture'}}})
    token=str(uuid.uuid4())
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        adapter=FeishuBitableAdapter('appMock001','tblMock001',lambda:'synthetic',enabled=True,http=http)
        result=adapter.create({'field':'value'},token)
        assert result['record_id']=='recFixture' and calls[0].url.params['client_token']==token
        assert calls[0].url.host=='open.feishu.cn' and len(calls)==1
    def broken(r):raise httpx.ReadTimeout('never expose raw details')
    with httpx.Client(transport=httpx.MockTransport(broken)) as http:
        adapter=FeishuBitableAdapter('appMock001','tblMock001',lambda:'synthetic',enabled=True,http=http)
        with pytest.raises(CRMError,match='submission_unknown'):adapter.create({'f':'x'},token)
    with pytest.raises(CRMError,match='field_mapping_invalid'):map_fields({}, {'external_key':'only one field'})


@pytest.mark.parametrize('replacement',['不需要，不要联系','拍得很好'])
def test_comment_loses_intent_revokes_previous_approval(local_project,fixture,replacement):
    account,work,_=fixture
    cid=store(CommentRecord(platform='douyin',watch_id=0,aweme_id='fixture-item',comment_id='a',text='想了解费用'))
    async def run():
        async with client() as http:
            body={'account_id':account,'work_id':work}
            await post(http,'/api/leads/propose',body)
            row=(await http.get('/api/leads')).json()['items'][0];lid=row['id']
            old={k:row[k] for k in ('version','digest')}
            await post(http,f'/api/leads/{lid}/review',{**old,'decision':'approve','owner_label':'owner','note':'reviewed'},'intent-reviewed-0001')
            with db.get_session() as s:
                c=s.get(CommentRecord,cid);c.text=replacement;s.add(c);s.commit()
            scan=await post(http,'/api/leads/propose',body,'intent-rescan-000001')
            assert scan.json()['matched']==0 and scan.json()['updated']==1
            changed=(await http.get(f'/api/leads/{lid}')).json()
            assert changed['state']=='candidate' and changed['version']==2 and changed['signal_tags']==[]
            assert (await http.get(f'/api/leads/{lid}/crm-preview')).status_code==409
    asyncio.run(run())
