"""Offline acceptance: Hub is authoritative; no live browser/platform calls."""
import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlmodel import select

import app.db as db
import app.main as main
from app.models import (DouyinAccount, PublishTask, RiskAdminAudit,
                        AccountWork, SyncRequest, SyncPacing)
from app.publish_review import media_intact, review_valid
from app.sync_requests import process_one, recover
from app.engine.monitor import MonitorEngine
from test_project_optimizations import local_project, store
from test_access_and_submissions import client


@pytest.fixture
def fixture(local_project, monkeypatch):
    account = store(DouyinAccount(platform="douyin", nickname="offline-test", storage_state="{}"))
    work = store(AccountWork(platform="douyin", account_id=account, item_id="fixture-item"))
    original = local_project.root / "fixture.jpg"
    original.write_bytes(b"offline media fixture")
    monkeypatch.setattr(main, "UPLOAD_DIR", local_project.root / "uploads")
    return account, work, {"account_id": account, "media_type": "images",
        "media_paths": [str(original)], "title": "离线验收", "desc": "不真实发布"}


def post(http, route, body, key="fixture-request-0001"):
    return http.post(route, json=body, headers={"Idempotency-Key": key})


def test_draft_is_not_executable_and_receipt_is_durable(local_project, fixture):
    account, _, body = fixture
    async def run():
        async with client() as http:
            response = await post(http, "/api/publish/drafts", body)
            assert response.status_code == 200, response.text
            row = response.json()
            assert row["status"] == "draft" and row["review_required"]
            assert len(row["review_digest"]) == 64
            assert "media_json" not in row and "storage_state" not in row
            task_id = row["id"]
            with db.get_session() as session:
                task = session.get(PublishTask, task_id)
                assert task.approved_at is None and media_intact(task) and not review_valid(task)
                copied = json.loads(task.media_manifest)[0]["path"]
                assert copied != body["media_paths"][0]
            # The original can change without altering the immutable Hub snapshot.
            from pathlib import Path
            Path(body["media_paths"][0]).write_bytes(b"changed original")
            replay = await post(http, "/api/publish/drafts", body)
            assert replay.json()["id"] == task_id and replay.json()["replayed"]
            db._engine.dispose(); db.init_db(str(local_project.path))
            assert (await post(http, "/api/publish/drafts", body)).json()["id"] == task_id
            engine = MonitorEngine(local_project.cfg, local_project.browser)
            engine.publish_task = AsyncMock()
            await engine._process_publish()
            engine.publish_task.assert_not_awaited()
            result = await engine._publish_task_locked(task_id)
            assert not result["ok"]
            status = (await http.get(f"/api/publish/{task_id}")).json()
            assert status["account_name"] == "offline-test" and status["media"][0]["sha256"]
            assert "path" not in status["media"][0]
            assert (await http.get(f"/api/publish/{task_id}/media/0")).content == b"offline media fixture"
            conflict = await post(http, "/api/publish/drafts", {**body, "desc":"other"})
            assert conflict.status_code == 409
    asyncio.run(run())


def test_approval_is_version_bound_idempotent_and_edit_revokes(local_project, fixture):
    _, _, body = fixture
    async def run():
        async with client() as http:
            task = (await post(http, "/api/publish/drafts", body)).json()
            tid = task["id"]
            approve_body = {k: task[k] for k in ("review_version", "review_digest")}
            assert (await post(http, f"/api/publish/{tid}/approve", {**approve_body,"review_digest":"0"*64},"fixture-bad-approve")).status_code == 409
            approved = await post(http, f"/api/publish/{tid}/approve", approve_body,"fixture-approve-0001")
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "pending"
            assert (await post(http, f"/api/publish/{tid}/approve", approve_body,"fixture-approve-0001")).json()["replayed"]
            assert (await post(http, f"/api/publish/{tid}/approve", approve_body,"fixture-approve-0002")).status_code == 409
            with db.get_session() as session:
                assert review_valid(session.get(PublishTask, tid))
                assert len(session.exec(select(RiskAdminAudit).where(RiskAdminAudit.action=="publish_draft_approved")).all()) == 1
            assert (await http.put(f"/api/publish/{tid}",json={"desc":"updated"})).status_code == 409
            edited = await http.put(f"/api/publish/{tid}",json={"desc":"updated","review_version":1})
            assert edited.status_code == 200
            assert edited.json()["status"] == "draft" and edited.json()["review_version"] == 2
            assert edited.json()["approved_at"] is None
            assert (await http.put(f"/api/publish/{tid}",json={"desc":"stale","review_version":1})).status_code == 409
            assert (await post(http,f"/api/publish/{tid}/approve",approve_body,"fixture-approve-0003")).status_code == 409
    asyncio.run(run())


def test_changed_media_and_account_cannot_pass_review(local_project, fixture):
    _, _, body = fixture
    async def run():
        async with client() as http:
            task=(await post(http,"/api/publish/drafts",body)).json(); tid=task['id']
            with db.get_session() as s:
                t=s.get(PublishTask,tid)
                from pathlib import Path
                Path(json.loads(t.media_manifest)[0]['path']).write_bytes(b'tampered snapshot')
            approve={k:task[k] for k in ('review_version','review_digest')}
            assert (await post(http,f'/api/publish/{tid}/approve',approve,'fixture-approve-media')).status_code==409
    asyncio.run(run())


def test_worker_rechecks_approved_digest_before_any_browser(local_project, fixture):
    _, _, body=fixture
    async def run():
        async with client() as http:
            task=(await post(http,'/api/publish/drafts',body)).json(); tid=task['id']
            approve={k:task[k] for k in ('review_version','review_digest')}
            await post(http,f'/api/publish/{tid}/approve',approve,'fixture-approve-0001')
            with db.get_session() as s:
                t=s.get(PublishTask,tid);t.desc='out of band modification';s.add(t);s.commit()
            engine=MonitorEngine(local_project.cfg,local_project.browser)
            result=await engine._publish_task_locked(tid)
            assert result['error']=='review_required'
            with db.get_session() as s:assert s.get(PublishTask,tid).status=='draft'
    asyncio.run(run())


@pytest.mark.parametrize('changes',[
    {'media_paths':[]},{'media_type':'video','media_paths':['/not/a/file.mp4','/other.mp4']},
    {'media_paths':['https://example.test/file.jpg']},{'title':'x'*21},{'visibility':'oops'},
    {'status':'pending'},{'review_required':False},
])
def test_draft_rejects_malformed_or_executable_inputs(local_project,fixture,changes):
    _,_,body=fixture
    async def run():
        async with client() as http:
            assert (await post(http,'/api/publish/drafts',{**body,**changes})).status_code==422
    asyncio.run(run())


def test_no_key_no_side_effect_and_platform_paths_still_exist(local_project,fixture):
    _,_,body=fixture
    async def run():
        async with client() as http:
            assert (await http.post('/api/publish/drafts',json=body)).status_code==422
            assert (await http.get('/api/publish/published')).status_code==422 # missing account, not integer tid conversion
            assert (await http.get('/api/publish/note-media')).status_code==422
        with db.get_session() as s:assert not s.exec(select(PublishTask)).all()
    asyncio.run(run())


def test_sync_persists_deduplicates_and_shares_hub_queue(local_project,fixture):
    account,work,_=fixture
    async def run():
        async with client() as http:
            body={'account_id':account,'kind':'comments','work_id':work}
            first=await post(http,'/api/sync-requests',body)
            assert first.status_code==200,first.text
            tid=first.json()['id']
            assert (await post(http,'/api/sync-requests',body)).json()['replayed']
            second=await post(http,'/api/sync-requests',body,'fixture-sync-another')
            assert second.json()['id']==tid and second.json()['deduplicated']
            items=(await http.get('/api/task-queue?queue_type=syncs&state=all')).json()['items']
            assert len(items)==1 and items[0]['queue_type']=='syncs'
            with db.get_session() as s:assert s.get(SyncRequest,tid).status=='pending'
            handler=AsyncMock(return_value={'ok':True,'fetched':2,'added':1})
            await process_one({'comments':handler})
            handler.assert_awaited_once_with(work)
            status=(await http.get(f'/api/sync-requests/{tid}')).json()
            assert status['status']=='done' and status['added']==1
    asyncio.run(run())


def test_sync_enforces_persistent_global_and_account_gaps(local_project,fixture):
    account,_,_=fixture
    second=store(DouyinAccount(platform='douyin',storage_state='{}'))
    async def run():
        async with client() as http:
            for i,aid in enumerate([account,second]):
                assert (await post(http,'/api/sync-requests',{'account_id':aid,'kind':'works'},f'fixture-sync-gap-{i:04d}')).status_code==200
            handler=AsyncMock(return_value={'ok':True})
            await process_one({'works':handler})
            db._engine.dispose();db.init_db(str(local_project.path))
            await process_one({'works':handler})
            assert handler.await_count==1
            with db.get_session() as s:
                pending=s.exec(select(SyncRequest).where(SyncRequest.status=='pending')).one()
                assert pending.next_allowed_at>datetime.utcnow() and pending.blocked_reason
                assert s.get(SyncPacing,account).next_allowed_at>datetime.utcnow()+timedelta(minutes=29)
    asyncio.run(run())


def test_sync_wrong_account_no_network_and_cancel_safe(local_project,fixture):
    account,work,_=fixture
    other=store(DouyinAccount(platform='douyin',storage_state='{}'))
    async def run():
        async with client() as http:
            assert (await post(http,'/api/sync-requests',{'account_id':other,'kind':'comments','work_id':work})).status_code==422
            assert (await post(http,'/api/sync-requests',{'account_id':account,'kind':'works','work_id':work})).status_code==422
            row=(await post(http,'/api/sync-requests',{'account_id':account,'kind':'works'})).json()
            result=await http.post('/api/task-queue/batch-stop',json={'items':[{'queue_type':'syncs','id':row['id']}]})
            assert result.status_code==200 and result.json()['stopped_count']==1
            handler=AsyncMock()
            await process_one({'works':handler});handler.assert_not_awaited()
    asyncio.run(run())


def test_sync_deferral_bounded_and_restart_never_blindly_replays(local_project,fixture):
    account,_,_=fixture
    tid=store(SyncRequest(platform='douyin',account_id=account,kind='works',active_key='test',attempts=2))
    async def run():
        handler=AsyncMock(return_value={'ok':True,'skipped':True,'source':'deferred'})
        await process_one({'works':handler})
        with db.get_session() as s:
            row=s.get(SyncRequest,tid);assert row.status=='failed' and row.active_key is None
    asyncio.run(run())
    interrupted=store(SyncRequest(platform='douyin',account_id=account,kind='works',active_key='other',status='running'))
    assert recover()==1
    with db.get_session() as s:
        row=s.get(SyncRequest,interrupted);assert row.status=='failed' and row.active_key is None


def test_parallel_submission_dedup(local_project,fixture):
    account,_,body=fixture
    def submit(key):
        async def run():
            async with client() as http:return await post(http,'/api/sync-requests',{'account_id':account,'kind':'works'},key)
        return asyncio.run(run())
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows=list(pool.map(submit,['fixture-parallel-0001','fixture-parallel-0002','fixture-parallel-0001']))
    assert all(r.status_code==200 for r in rows),[r.text for r in rows]
    assert len({r.json()['id'] for r in rows})==1


def test_parallel_approval_one_wins_and_never_executes(local_project,fixture):
    _,_,body=fixture
    async def create():
        async with client() as http:return (await post(http,'/api/publish/drafts',body)).json()
    task=asyncio.run(create());tid=task['id']
    approve={k:task[k] for k in ('review_version','review_digest')}
    def submit(key):
        async def run():
            async with client() as http:return await post(http,f'/api/publish/{tid}/approve',approve,key)
        return asyncio.run(run())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(submit,['fixture-approve-race-01','fixture-approve-race-02']))
    assert sorted(r.status_code for r in responses)==[200,409]
    with db.get_session() as s:assert s.get(PublishTask,tid).status=='pending'


def test_publish_business_stagger_and_late_hold(local_project,fixture):
    from app.publish_review import publish_slot
    from app.models import PublishPacing
    account,_,_=fixture
    now=datetime.utcnow()
    task=PublishTask(account_id=account,review_required=True,status='pending',approved_at=now-timedelta(minutes=31),approved_digest='a'*64,review_digest='a'*64)
    tid=store(task)
    engine=MonitorEngine(local_project.cfg,local_project.browser)
    result=asyncio.run(engine._publish_task_locked(tid))
    assert result['error']=='review_expired'
    with db.get_session() as s:
        row=s.get(PublishTask,tid);assert row.status=='draft' and row.approved_at is None
        assert publish_slot(s,row,now,reserve=True) is None;s.commit()
    with db.get_session() as s:
        assert publish_slot(s,PublishTask(account_id=account),now+timedelta(minutes=1))==now+timedelta(minutes=30)
        other=PublishTask(account_id=account+100)
        assert publish_slot(s,other,now+timedelta(minutes=1))==now+timedelta(minutes=10)


def test_future_reviewed_publish_cannot_run_early(local_project,fixture):
    account,_,_=fixture
    tid=store(PublishTask(account_id=account,review_required=True,status='pending',scheduled_at=datetime.utcnow()+timedelta(hours=1),approved_at=datetime.utcnow()))
    engine=MonitorEngine(local_project.cfg,local_project.browser)
    assert '预约时间' in asyncio.run(engine._publish_task_locked(tid))['error']


def test_sync_revalidates_platform_item_not_just_local_work_id(local_project,fixture):
    account,work,_=fixture
    async def run():
        async with client() as http:
            task=(await post(http,'/api/sync-requests',{'account_id':account,'kind':'comments','work_id':work})).json()
            with db.get_session() as s:
                row=s.get(AccountWork,work);row.item_id='a different platform item';s.add(row);s.commit()
            handler=AsyncMock(return_value={'ok':True})
            await process_one({'comments':handler});handler.assert_not_awaited()
            status=(await http.get(f'/api/sync-requests/{task["id"]}')).json()
            assert status['status']=='failed'
    asyncio.run(run())


def test_two_processors_cannot_burst_global_queue(local_project,fixture):
    account,_,_=fixture
    other=store(DouyinAccount(platform='douyin',storage_state='{}'))
    async def run():
        async with client() as http:
            for aid in [account,other]:
                await post(http,'/api/sync-requests',{'account_id':aid,'kind':'works'},f'fixture-sync-worker-{aid}')
        handler=AsyncMock(return_value={'ok':True})
        await asyncio.gather(process_one({'works':handler}),process_one({'works':handler}))
        assert handler.await_count==1
    asyncio.run(run())


@pytest.mark.parametrize('outcome',['done','uncertain','concurrent_edit'])
def test_reviewed_draft_uses_real_engine_transition_without_db_lock_or_replay(local_project,fixture,monkeypatch,outcome):
    from test_write_gates import _BrowserStub
    account,_,body=fixture
    local_project.cfg.engine.quiet_hours_enabled=False
    async def run():
        async with client() as http:
            task=(await post(http,'/api/publish/drafts',body)).json();tid=task['id']
            approval={k:task[k] for k in ('review_version','review_digest')}
            assert (await post(http,f'/api/publish/{tid}/approve',approval,'fixture-approve-engine')).status_code==200
            engine=MonitorEngine(local_project.cfg,_BrowserStub())
            adapter=AsyncMock(return_value=(True,'https://example.test/offline','') if outcome=='done' else (False,'','write_uncertain:fixture lost response'))
            monkeypatch.setattr('app.engine.monitor.publish_douyin',adapter)
            if outcome=='concurrent_edit':
                def edit_during_preflight(_):
                    with db.get_session() as s:
                        t=s.get(PublishTask,tid);t.review_version+=1;t.status='draft';t.approved_at=None;s.add(t);s.commit()
                    return ''
                monkeypatch.setattr(engine,'_write_pause_error',edit_during_preflight)
            result=await engine.publish_task(tid)
            with db.get_session() as s:status=s.get(PublishTask,tid).status
            if outcome=='concurrent_edit':
                adapter.assert_not_awaited();assert status=='draft' and not result['ok']
            else:
                assert status==outcome and adapter.await_count==1
                await engine.publish_task(tid)
                assert adapter.await_count==1
                if outcome=='uncertain':
                    await engine._process_publish();assert adapter.await_count==1
    asyncio.run(run())
