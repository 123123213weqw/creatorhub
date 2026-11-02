"""Isolated DB and mocked upstream only. Never load a real credential."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from unittest.mock import Mock

from fastapi import HTTPException
import pytest
from sqlmodel import select

import app.db as db
import app.wecom_workflows as w
from app.models import AppSetting
from app.wecom_readonly import WeComError
from test_project_optimizations import local_project
from test_access_and_submissions import client

KEY = 'wecom-test-request-000001'


def valid_report(count=1):
    return {'status':'ok','application_enabled':True,'visible_member_count':1,
        'enabled_member_count':1,'customer_members_api_verified':True,
        'customer_list_api_verified':True,'customer_ids_returned':count,
        'customer_details_read':False,'chat_content_read':False,'raw_customer_ids_saved':False,
        'messages_sent':0,'distribution_tasks_created':0,'moments_tasks_created':0,
        'external_userid':['PRIVATE_CUSTOMER'],'secret':'PRIVATE_SECRET'}


@pytest.fixture
def setup(local_project, monkeypatch):
    clock = [1800000000.0]
    monkeypatch.setattr(w, '_clock', lambda: clock[0])
    api = Mock();api.probe.return_value = valid_report()
    factory = Mock(return_value=api);monkeypatch.setattr(w, '_factory', factory)
    return clock, api, factory


def test_gets_never_load_credentials_or_refresh_and_cache_unknown_not_zero(setup):
    _, _, factory = setup
    async def run():
        async with client() as h:
            r = await h.get('/api/integrations/wecom/status')
            assert r.status_code == 200 and r.json()['last_success'] is None
            assert r.json()['cache_present'] is False
            assert (await h.get('/api/integrations/wecom/requests/'+KEY)).status_code == 404
    asyncio.run(run());factory.assert_not_called()


def test_success_counts_projection_replay_and_persistent_pacing(setup):
    clock, api, factory = setup
    r = w.refresh(KEY)
    assert r['refresh']['status'] == 'completed' and r['last_success']['counts']['customer_count'] == 1
    assert r['platform_refresh_triggered'] is True and 'PRIVATE' not in json.dumps(r)
    with db.get_session() as s:
        raw = '\n'.join(x.value for x in s.exec(select(AppSetting)).all())
    assert 'PRIVATE' not in raw
    path=db._engine.url.database
    db._engine.dispose();db.init_db(str(path))
    r2 = w.refresh(KEY)
    assert r2['replayed'] and not r2['platform_refresh_triggered']
    with pytest.raises(HTTPException) as e:w.refresh(KEY+'-different')
    assert e.value.status_code == 429 and factory.call_count == 1
    api.close.assert_called_once()
    assert w.request_status(KEY)['refresh']['status'] == 'completed'
    clock[0] += 3601
    assert w.snapshot()['cache_fresh'] is False
    assert w.refresh(KEY)['cache_fresh'] is False


def test_failed_refresh_retains_prior_success_and_never_leaks_errors(setup):
    clock, api, _ = setup
    w.refresh(KEY);clock[0] += w.MIN_INTERVAL
    api.probe.side_effect = WeComError('wecom_api_rejected', 60020)
    r = w.refresh(KEY+'-new')
    assert r['refresh']['status'] == 'failed' and r['refresh']['api_code'] == 60020
    assert r['last_success']['counts']['customer_count'] == 1
    assert r['last_attempt']['status'] == 'failed'
    clock[0] += w.MIN_INTERVAL
    api.probe.side_effect = RuntimeError('PRIVATE_SECRET https://example/?token=PRIVATE')
    r = w.refresh(KEY+'-third')
    assert r['refresh']['error_code'] == 'wecom_probe_failed' and 'PRIVATE' not in json.dumps(r)


@pytest.mark.parametrize('report', [
    valid_report(True),valid_report(-1),{**valid_report(),'visible_member_count':2},
    {**valid_report(),'chat_content_read':True},{**valid_report(),'raw_customer_ids_saved':True},
    {**valid_report(),'messages_sent':True},{**valid_report(),'customer_list_api_verified':False},
])
def test_scope_contract_fails_closed_without_cache(setup, report):
    _, api, _ = setup;api.probe.return_value = report
    r = w.refresh(KEY)
    assert r['refresh']['status'] == 'failed' and r['last_success'] is None


def test_missing_relationship_not_successful_zero(setup):
    _, api, _ = setup;api.probe.return_value={'status':'no_external_contact_relationship'}
    r=w.refresh(KEY)
    assert r['refresh']['api_code']==84061 and r['last_success'] is None


def test_concurrent_claim_and_receipt_without_holding_db_over_network(setup):
    _, api, factory = setup
    entered, release = threading.Event(), threading.Event()
    def blocked(**kwargs):
        entered.set();assert release.wait(5);return valid_report()
    api.probe.side_effect=blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(w.refresh,KEY);assert entered.wait(5)
        assert w.request_status(KEY)['refresh']['status']=='running'
        assert w.refresh(KEY)['refresh']['status']=='running'
        with pytest.raises(HTTPException) as e:w.refresh(KEY+'-other')
        assert e.value.status_code==429
        release.set();assert first.result(5)['refresh']['status']=='completed'
    assert factory.call_count==1


def test_crash_pending_receipt_is_not_automatically_retried(setup):
    clock, _, factory = setup
    pending={'status':'running','started':clock[0]}
    with db.get_session() as s:
        w._write(s,w.STATE,{'claim':w._digest(KEY),'next_allowed':clock[0]+w.MIN_INTERVAL,
                            'last_attempt':pending,'last_success':None})
        w._write(s,w.RECEIPT+w._digest(KEY),pending);s.commit()
    clock[0] += w.MIN_INTERVAL+1
    assert w.refresh(KEY)['refresh']['status']=='interrupted'
    assert not w.snapshot()['automatic_polling_enabled'];factory.assert_not_called()


def test_clock_rollback_marks_cache_stale_and_keeps_cooldown(setup):
    clock, _, _=setup;w.refresh(KEY);clock[0]-=1
    assert w.snapshot()['clock_skew_detected'] and not w.snapshot()['cache_fresh']
    with pytest.raises(HTTPException) as e:w.refresh(KEY+'-new')
    assert e.value.status_code==429


@pytest.mark.parametrize('body', [{},{'acknowledge_customer_ids':False},
    {'acknowledge_customer_ids':1},{'acknowledge_customer_ids':True,'userid':'PRIVATE'}])
def test_strict_ack_before_external_calls(setup, body):
    _, _, factory=setup
    async def run():
        async with client() as h:
            r=await h.post('/api/integrations/wecom/refresh',json=body,headers={'Idempotency-Key':KEY})
            assert r.status_code==422
    asyncio.run(run());factory.assert_not_called()


def test_routes_remote_team_origin_and_admin_boundaries(setup, monkeypatch):
    _, _, factory=setup
    async def run():
        async with client(peer='192.0.2.8') as h:
            assert (await h.get('/api/integrations/wecom/status')).status_code==403
        async with client() as h:
            for headers in [{'Authorization':'Bearer cht_'+'a'*32},{'Origin':'https://evil.example'},
                            {'X-CreatorHub-Team-Gateway':'untrusted'}]:
                assert (await h.get('/api/integrations/wecom/status',headers=headers)).status_code==403
            monkeypatch.setenv('CREATORHUB_ADMIN_TOKEN','test-owner-token')
            assert (await h.get('/api/integrations/wecom/status')).status_code==403
            assert (await h.get('/api/integrations/wecom/status',headers={'X-CreatorHub-Admin-Token':'test-owner-token'})).status_code==200
    asyncio.run(run());factory.assert_not_called()


def test_invalid_key_or_cache_does_not_call_platform(setup):
    _, _, factory=setup
    for key in ['', 'short', '../escape', 'x'*129]:
        with pytest.raises(HTTPException):w.refresh(key)
    with db.get_session() as s:
        s.add(AppSetting(key=w.STATE,value='invalid-json'));s.commit()
    with pytest.raises(HTTPException):w.refresh(KEY)
    factory.assert_not_called()


def test_api_success_receipt_and_health_contract(setup):
    async def run():
        async with client() as h:
            assert (await h.get('/health')).json()['wecom_readonly_protocol']=='1.0'
            r=await h.post('/api/integrations/wecom/refresh',json={'acknowledge_customer_ids':True},headers={'Idempotency-Key':KEY})
            assert r.status_code==200 and r.json()['refresh']['status']=='completed'
            assert (await h.get('/api/integrations/wecom/requests/'+KEY)).json()['refresh']['status']=='completed'
            html=(await h.get('/')).text
            assert 'wecomRefreshCounts' in html and '/static/wecom.js' in html
    asyncio.run(run())
