"""Synthetic official responses + isolated DB only. Never tenant acceptance."""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import threading

import httpx
import pytest
from sqlmodel import select

import app.db as db
import app.wecom_moments as flow
from app.business_models import WeComMomentTask
from app.models import AppSetting, LocalMediaAsset
from app.wecom_moments_client import WeComMomentsClient, MomentWriteError
from app.wecom_readonly import Credentials, WeComError
from test_access_and_submissions import client
from test_project_optimizations import local_project, store

ROOT = '/api/integrations/wecom/moments'


@pytest.fixture
def pilot(local_project, monkeypatch):
    import app.main as main
    root = local_project.root / 'uploads'
    root.mkdir()
    monkeypatch.setattr(main, 'UPLOAD_DIR', root)
    state = {'calls': [], 'published': 0, 'job_status': 3, 'fault': '', 'scope': 'fixture-member',
             'invalid_tags': False, 'invalid_member': False, 'uploads': 0}
    def handler(request):
        path = request.url.path
        body = json.loads(request.content) if request.content and request.headers.get('content-type', '').startswith('application/json') else {}
        state['calls'].append((request.method, path, body))
        if path.endswith('/gettoken'):
            data = {'access_token': 'fixture-token', 'expires_in': 7200}
        elif path.endswith('/agent/get'):
            data = {'agentid': 1000002, 'close': 0, 'allow_userinfos': {'user': [{'userid': state['scope']}]},
                    'allow_partys': {'partyid': []}, 'allow_tags': {'tagid': []}}
        elif path.endswith('/media/upload_attachment'):
            state['uploads'] += 1
            if state['fault'] == 'upload_timeout':
                raise httpx.ReadTimeout('PRIVATE_TOKEN_AND_BODY')
            assert request.url.params['attachment_type'] == '1'
            assert b'filelength=' in request.content
            data = {'media_id': 'fixture-media-' + str(state['uploads']), 'created_at': 1700000000,
                    'type': request.url.params['media_type']}
        elif path.endswith('/externalcontact/add_moment_task'):
            if state['fault'] == 'create_timeout':
                raise httpx.ReadTimeout('PRIVATE_TOKEN_AND_BODY')
            if state['fault'] == 'create_rejected':
                return httpx.Response(200, json={'errcode': 48002, 'errmsg': 'PRIVATE_BODY'})
            if state['fault'] == 'create_5xx':
                return httpx.Response(503, json={'errcode': 0, 'jobid': 'maybe-created'})
            if state['fault'] == 'missing_job':
                return httpx.Response(200, json={'errcode': 0})
            data = {'jobid': 'fixture-job'}
        elif path.endswith('/externalcontact/get_moment_task_result'):
            if state['fault'] == 'query_error':
                return httpx.Response(200, json={'errcode': 48002, 'errmsg': 'PRIVATE_BODY'})
            data = {'status': state['job_status'], 'type': 'add_moment_task'}
            if state['job_status'] == 3:
                data['result'] = {'errcode': 0, 'moment_id': 'fixture-moment',
                    'invalid_sender_list': {'user_list': ['bad-member'] if state['invalid_member'] else []},
                    'invalid_external_contact_list': {'tag_list': ['bad-tag'] if state['invalid_tags'] else []}}
        elif path.endswith('/externalcontact/get_moment_task'):
            data = {'task_list': [] if state['published'] is None else [
                {'userid': 'fixture-member', 'publish_status': state['published']}], 'next_cursor': ''}
        else:
            pytest.fail('unallowlisted endpoint ' + path)
        return httpx.Response(200, json={'errcode': 0, **data})
    def factory():
        return WeComMomentsClient(Credentials('ww1234567890', 1000002, 'fixture-secret'),
                                 transport=httpx.MockTransport(handler), sleep=lambda _: None)
    monkeypatch.setattr(flow, 'factory', factory)
    state['root'] = root
    return state


def headers(key):
    return {'Idempotency-Key': 'wecom-moment-fixture-' + key}


async def prepare(http, *, demo=False, media_type='text', asset_ids=None, name='draft'):
    bound = await http.post(ROOT + '/preflight', json={}, headers=headers('binding'))
    assert bound.status_code == 200, bound.text
    assert bound.json()['status'] == 'ready'
    body = {'label': 'Synthetic fixture', 'binding_id': bound.json()['binding_id'],
        'text': 'Synthetic text, not a real customer communication.', 'media_type': media_type,
        'customer_tag_ids': ['etFIXTURE_TAG'], 'customer_scope_note': 'Controlled fixture customers only',
        'asset_ids': asset_ids or [], 'demo_only': demo}
    response = await http.post(ROOT + '/drafts', json=body, headers=headers(name))
    assert response.status_code == 200, response.text
    return response.json(), body


async def approve(http, draft, *, ack=True):
    return await http.post(ROOT + '/' + draft['id'] + '/review', headers=headers('review-' + draft['id']),
        json={'digest': draft['digest'], 'review_note': 'Owner-approved synthetic fixture.',
              'acknowledge_content': ack, 'acknowledge_customer_tags': True,
              'acknowledge_member_notification': True})


async def submit(http, draft, *, key='submit', ack=True):
    return await http.post(ROOT + '/' + draft['id'] + '/submit', headers=headers(key),
                           json={'digest': draft['digest'], 'acknowledge_external_writes': ack})


async def query(http, draft, key='query'):
    return await http.post(ROOT + '/' + draft['id'] + '/refresh', headers=headers(key),
                           json={'acknowledge_platform_read': True})


def release_query_gate(task_id):
    with db.get_session() as s:
        row = s.get(WeComMomentTask, task_id)
        row.last_query_at -= timedelta(seconds=61)
        s.add(row)
        s.commit()


def test_real_transport_contract_synthetic_complete_flow(pilot):
    async def run():
        async with client() as h:
            assert (await h.get(ROOT)).json()['items'] == []
            assert not pilot['calls']
            draft, body = await prepare(h)
            assert not draft['external_write_triggered'] and draft['status'] == 'draft'
            before = len(pilot['calls'])
            replay = await h.post(ROOT + '/drafts', json=body, headers=headers('draft'))
            assert replay.json()['id'] == draft['id'] and replay.json()['replayed']
            assert len(pilot['calls']) == before
            receipt = await h.get(ROOT + '/receipts/draft/wecom-moment-fixture-draft')
            assert receipt.json()['id'] == draft['id']
            assert (await approve(h, draft)).status_code == 200
            result = await submit(h, draft)
            assert result.status_code == 200, result.text
            assert result.json()['status'] == 'creating' and result.json()['job_known']
            assert result.json()['member_published'] is None
            posts = [x for x in pilot['calls'] if x[1].endswith('/add_moment_task')]
            assert len(posts) == 1
            assert posts[0][2]['visible_range'] == {
                'sender_list': {'user_list': ['fixture-member']},
                'external_contact_list': {'tag_list': ['etFIXTURE_TAG']}}
            assert (await submit(h, draft)).json()['replayed']
            assert (await submit(h, draft, key='another-submit')).json()['replayed']
            first = await query(h, draft)
            assert first.status_code == 200, first.text
            assert first.json()['status'] == 'awaiting_member'
            assert first.json()['member_published'] is False
            assert (await query(h, draft)).json()['replayed']
            assert (await query(h, draft, 'query-too-soon')).status_code == 429
            release_query_gate(draft['id'])
            pilot['published'] = 1
            done = (await query(h, draft, 'query-2')).json()
            assert done['status'] == 'published' and done['member_published']
            assert done['read_count'] is done['reply_count'] is done['sales_count'] is None
            before = len(pilot['calls'])
            assert (await h.get(ROOT + '/' + draft['id'])).json()['moment_id'] == 'fixture-moment'
            assert (await h.get(ROOT)).json()['items'][0]['status'] == 'published'
            assert len(pilot['calls']) == before
            assert len([x for x in pilot['calls'] if x[1].endswith('/add_moment_task')]) == 1
    asyncio.run(run())


@pytest.mark.parametrize('fault,expected', [('create_timeout', 'unknown'), ('create_5xx', 'unknown'),
    ('missing_job', 'unknown'), ('create_rejected', 'rejected')])
def test_write_failure_never_retries_or_resets(pilot, fault, expected):
    pilot['fault'] = fault
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            assert (await approve(h, d)).status_code == 200
            response = await submit(h, d)
            assert response.status_code == 200, response.text
            assert response.json()['status'] == expected and response.json()['task_creation_attempts'] == 1
            assert 'PRIVATE' not in response.text
            assert (await submit(h, d)).json()['replayed']
            assert (await submit(h, d, key='new-number')).json()['replayed']
            assert (await approve(h, d)).json()['replayed']  # Old local receipt, not a reapproval.
            assert (await query(h, d)).status_code == 409
            assert len([x for x in pilot['calls'] if x[1].endswith('/add_moment_task')]) == 1
    asyncio.run(run())


@pytest.mark.parametrize('change', ['member', 'digest', 'expired', 'future_approval', 'demo', 'unapproved'])
def test_no_submit_on_scope_content_or_approval_problem(pilot, change):
    async def run():
        async with client() as h:
            d, _ = await prepare(h, demo=change == 'demo')
            if change != 'unapproved':
                approved = await approve(h, d)
                if change == 'demo':
                    assert approved.status_code == 409
                else:
                    assert approved.status_code == 200
            if change == 'member':
                pilot['scope'] = 'unexpected-member'
            if change in {'digest', 'expired', 'future_approval'}:
                with db.get_session() as s:
                    row = s.get(WeComMomentTask, d['id'])
                    if change == 'digest': row.text = 'altered after review'
                    else: row.approved_at += timedelta(hours=-2 if change == 'expired' else 2)
                    s.add(row)
                    s.commit()
            response = await submit(h, d)
            assert response.status_code in {200, 409}
            if response.status_code == 200:
                assert response.json()['status'] == 'failed_before_task'
            assert not any(x[1].endswith('/add_moment_task') for x in pilot['calls'])
    asyncio.run(run())


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(customer_tag_ids=[]),
    lambda b: b.update(customer_tag_ids=['anything']),
    lambda b: b.update(customer_tag_ids=['etABC', 'etABC']),
    lambda b: b.update(customer_scope_note=' '),
    lambda b: b.update(text=' '),
    lambda b: b.update(text='汉' * 1500),
    lambda b: b.update(asset_ids=['asset_invalid']),
    lambda b: b.update(media_type='link'),
    lambda b: b.update(sender_list={'department_list': [1]}),
])
def test_invalid_draft_and_no_all_customers_fallback(pilot, mutation):
    async def run():
        async with client() as h:
            _, body = await prepare(h)
            mutation(body)
            result = await h.post(ROOT + '/drafts', json=body, headers=headers('invalid-draft'))
            assert result.status_code == 422, result.text
            assert not any(x[1].endswith('/add_moment_task') for x in pilot['calls'])
    asyncio.run(run())


def test_missing_ack_and_privilege_boundaries(pilot):
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            assert (await approve(h, d, ack=False)).status_code == 422
            assert (await submit(h, d, ack=False)).status_code == 422
            for path, method in [('', 'GET'), ('/binding', 'GET'), ('/preflight', 'POST'),
                                 ('/drafts', 'POST'), ('/' + d['id'], 'GET')]:
                result = await h.request(method, ROOT + path, headers={'Authorization': 'Bearer cht_test'})
                assert result.status_code == 403
        async with client(peer='192.0.2.1') as h:
            assert (await h.get(ROOT)).status_code == 403
        async with client() as h:
            assert (await h.post(ROOT + '/preflight', headers={'Origin': 'https://evil.invalid'})).status_code == 403
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['invalid_tags', 'invalid_member'])
def test_async_scope_rejection_is_not_success_or_new_task(pilot, kind):
    pilot[kind] = True
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            await approve(h, d)
            await submit(h, d)
            r = (await query(h, d)).json()
            assert r['status'] == 'scope_rejected' and r['creation_scope_rejected']
            assert r['moment_known'] and r['member_published'] is None
            assert not any(x[1].endswith('/get_moment_task') for x in pilot['calls'])
    asyncio.run(run())


def test_query_failure_preserves_creation_and_later_empty_does_not_revoke(pilot):
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            await approve(h, d)
            await submit(h, d)
            pilot['fault'] = 'query_error'
            r = (await query(h, d)).json()
            assert r['status'] == 'creating' and r['last_query_api_code'] == 48002
            assert 'PRIVATE' not in json.dumps(r)
            release_query_gate(d['id'])
            pilot['fault'], pilot['published'] = '', 1
            assert (await query(h, d, 'query-2')).json()['status'] == 'published'
            release_query_gate(d['id'])
            pilot['published'] = None
            r = (await query(h, d, 'query-3')).json()
            assert r['status'] == 'published' and r['member_published'] is True
            assert r['last_query_error'] == 'wecom_moment_execution_result_changed_requires_check'
    asyncio.run(run())


def test_restart_no_external_io_and_unknown_post_not_reset(pilot):
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            with db.get_session() as s:
                row = s.get(WeComMomentTask, d['id'])
                row.status, row.task_creation_attempts, row.query_nonce = 'submitting', 1, 'fixture-nonce'
                s.add(row)
                s.commit()
            n = len(pilot['calls'])
            assert flow.recover() == 2 and len(pilot['calls']) == n
            row = (await h.get(ROOT + '/' + d['id'])).json()
            assert row['status'] == 'unknown'
    asyncio.run(run())


def image_asset(pilot, *, demo=False, fmt='PNG', size=(96, 160)):
    from PIL import Image
    import hashlib
    path = pilot['root'] / ('fixture.png' if fmt == 'PNG' else 'fixture.jpg')
    Image.new('RGB', size, 'white').save(path, format=fmt)
    raw = path.read_bytes()
    aid = 'asset_' + 'a' * 32
    store(LocalMediaAsset(id=aid, label='fixture', media_type='images', is_demo=demo,
        manifest_json=json.dumps([{'path': str(path), 'name': path.name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}])))
    return aid, path


def test_image_snapshot_upload_and_review_media(pilot):
    aid, path = image_asset(pilot)
    async def run():
        async with client() as h:
            d, _ = await prepare(h, media_type='images', asset_ids=[aid])
            detail = (await h.get(ROOT + '/' + d['id'])).json()
            assert detail['media'][0]['format'] == 'PNG' and 'path' not in detail['media'][0]
            assert (await h.get(detail['media'][0]['preview_url'])).content == path.read_bytes()
            path.write_bytes(b'original changed after snapshot')
            assert (await approve(h, d)).status_code == 200
            assert (await submit(h, d)).json()['status'] == 'creating'
            assert pilot['uploads'] == 1
            payload = next(x[2] for x in pilot['calls'] if x[1].endswith('/add_moment_task'))
            assert payload['attachments'] == [{'msgtype': 'image', 'image': {'media_id': 'fixture-media-1'}}]
    asyncio.run(run())


def test_demo_asset_and_mutated_snapshot_never_upload(pilot):
    aid, _ = image_asset(pilot, demo=True)
    async def run():
        async with client() as h:
            d, _ = await prepare(h, media_type='images', asset_ids=[aid])
            assert d['demo_only'] and (await approve(h, d)).status_code == 409
            with db.get_session() as s:
                row = s.get(WeComMomentTask, d['id'])
                path = Path(json.loads(row.media_manifest_json)[0]['path'])
            path.write_bytes(b'changed')
            assert (await h.get(ROOT + '/' + d['id'] + '/media/0')).status_code == 409
            assert pilot['uploads'] == 0
    asyncio.run(run())


def test_upload_timeout_does_not_attempt_task_creation(pilot):
    aid, _ = image_asset(pilot)
    pilot['fault'] = 'upload_timeout'
    async def run():
        async with client() as h:
            d, _ = await prepare(h, media_type='images', asset_ids=[aid])
            await approve(h, d)
            r = (await submit(h, d)).json()
            assert r['status'] == 'failed_before_task' and r['task_creation_attempts'] == 0
            assert r['media_upload_attempts'] == 1
            assert pilot['uploads'] == 1
            assert not any(x[1].endswith('/add_moment_task') for x in pilot['calls'])
    asyncio.run(run())


def test_preflight_original_receipt_and_throttle(pilot):
    async def run():
        async with client() as h:
            bound = await h.post(ROOT + '/preflight', json={}, headers=headers('binding'))
            n = len(pilot['calls'])
            receipt = await h.get(ROOT + '/receipts/preflight/wecom-moment-fixture-binding')
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()['binding_id'] == bound.json()['binding_id']
            assert len(pilot['calls']) == n
            assert (await h.post(ROOT + '/preflight', json={}, headers=headers('other'))).status_code == 429
            assert len(pilot['calls']) == n
    asyncio.run(run())


def test_concurrent_submissions_claim_exactly_one_post(pilot, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = WeComMomentsClient.create
    def blocked(self, body, before):
        started.set()
        assert release.wait(5)
        return original(self, body, before)
    monkeypatch.setattr(WeComMomentsClient, 'create', blocked)
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            await approve(h, d)
            first = asyncio.create_task(submit(h, d, key='concurrent-1'))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                others = await asyncio.gather(*(submit(h, d, key='concurrent-' + str(i)) for i in range(2, 10)))
                assert all(r.status_code == 200 and r.json()['status'] == 'submitting' and r.json()['replayed'] for r in others)
            finally:
                release.set()
            assert (await first).json()['status'] == 'creating'
            assert len([x for x in pilot['calls'] if x[1].endswith('/add_moment_task')]) == 1
    asyncio.run(run())


def test_cleanup_error_preserves_received_job(pilot, monkeypatch):
    original = WeComMomentsClient.close
    def bad_close(self):
        original(self)
        raise RuntimeError('PRIVATE cleanup failure')
    monkeypatch.setattr(WeComMomentsClient, 'close', bad_close)
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            await approve(h, d)
            assert (await submit(h, d)).json()['status'] == 'creating'
            assert (await query(h, d)).json()['status'] == 'awaiting_member'
    asyncio.run(run())


@pytest.mark.parametrize('job_status', [1, 2])
def test_in_progress_creation_never_implies_publication(pilot, job_status):
    pilot['job_status'] = job_status
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            await approve(h, d)
            await submit(h, d)
            r = (await query(h, d)).json()
            assert r['status'] == 'creating' and r['creation_job_status'] == job_status
            assert r['member_published'] is None and not r['moment_known']
            assert not any(x[1].endswith('/get_moment_task') for x in pilot['calls'])
    asyncio.run(run())


def test_expired_job_not_recreated_and_no_result_query(pilot):
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            await approve(h, d)
            await submit(h, d)
            with db.get_session() as s:
                row = s.get(WeComMomentTask, d['id'])
                row.job_received_at -= timedelta(hours=25)
                s.add(row)
                s.commit()
            r = (await query(h, d)).json()
            assert r['status'] == 'creating' and r['last_query_error'] == 'wecom_moment_job_expired_requires_manual_check'
            assert not any(x[1].endswith('/get_moment_task_result') for x in pilot['calls'])
            assert len([x for x in pilot['calls'] if x[1].endswith('/add_moment_task')]) == 1
    asyncio.run(run())


def test_cancel_is_local_and_unknown_submission_cannot_be_canceled(pilot):
    async def run():
        async with client() as h:
            d, _ = await prepare(h)
            before = len(pilot['calls'])
            result = await h.post(ROOT + '/' + d['id'] + '/cancel', json={'reason': 'fixture canceled'}, headers=headers('cancel'))
            assert result.json()['status'] == 'canceled' and len(pilot['calls']) == before
            assert (await approve(h, d)).status_code == 409
            d2, _ = await prepare(h, name='draft-2')
            await approve(h, d2)
            pilot['fault'] = 'create_timeout'
            assert (await submit(h, d2)).json()['status'] == 'unknown'
            result = await h.post(ROOT + '/' + d2['id'] + '/cancel', json={'reason': 'do not hide unknown'}, headers=headers('cancel-2'))
            assert result.status_code == 409
    asyncio.run(run())


@pytest.mark.parametrize('size', [(1081, 1081), (10801, 1)])
def test_invalid_image_dimensions_fail_before_any_upload(pilot, size):
    aid, _ = image_asset(pilot, size=size)
    async def run():
        async with client() as h:
            await h.post(ROOT + '/preflight', json={}, headers=headers('binding'))
            binding = (await h.get(ROOT + '/binding')).json()
            result = await h.post(ROOT + '/drafts', headers=headers('bad-image'), json={
                'label': 'bad fixture', 'binding_id': binding['binding_id'], 'media_type': 'images',
                'asset_ids': [aid], 'customer_tag_ids': ['etFIXTURE'], 'customer_scope_note': 'controlled'})
            assert result.status_code == 422
            assert pilot['uploads'] == 0
            assert not list((pilot['root'] / 'review-drafts').glob('**/*.png'))
    asyncio.run(run())


@pytest.mark.parametrize('duration,format_name,track,success', [
    ('30.0', 'mov,mp4,m4a,3gp,3g2,mj2', 'video', True),
    ('30.01', 'mp4', 'video', False), ('nan', 'mp4', 'video', False),
    ('1.0', 'matroska', 'video', False), ('1.0', 'mp4', 'audio', False)])
def test_video_contract_validated_without_transcode(pilot, monkeypatch, duration, format_name, track, success):
    import hashlib
    from types import SimpleNamespace
    import app.wecom_moments_media as media
    path = pilot['root'] / 'fixture.mp4'
    raw = b'synthetic-mp4-container-placeholder'
    path.write_bytes(raw)
    aid = 'asset_' + 'c' * 32
    store(LocalMediaAsset(id=aid, label='fixture video', media_type='video',
        manifest_json=json.dumps([{'path': str(path), 'name': path.name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}])))
    monkeypatch.setattr(media.shutil, 'which', lambda _: '/fixture/ffprobe')
    commands = []
    def probe(args, **kw):
        commands.append(args)
        return SimpleNamespace(stdout=json.dumps({'format': {'format_name': format_name, 'duration': duration}, 'streams': [{'codec_type': track}]}).encode())
    monkeypatch.setattr(media.subprocess, 'run', probe)
    async def run():
        async with client() as h:
            bound = (await h.post(ROOT + '/preflight', json={}, headers=headers('binding'))).json()
            result = await h.post(ROOT + '/drafts', headers=headers('video'), json={
                'label': 'video fixture', 'binding_id': bound['binding_id'], 'media_type': 'video',
                'asset_ids': [aid], 'customer_tag_ids': ['etFIXTURE'], 'customer_scope_note': 'controlled'})
            assert result.status_code == (200 if success else 422), result.text
            assert commands[0][0] == '/fixture/ffprobe' and 'file,pipe' in commands[0]
            if success:
                d = result.json()
                await approve(h, d)
                assert (await submit(h, d)).json()['status'] == 'creating'
                payload = next(x[2] for x in pilot['calls'] if x[1].endswith('/add_moment_task'))
                assert payload['attachments'] == [{'msgtype': 'video', 'video': {'media_id': 'fixture-media-1'}}]
            else:
                assert pilot['uploads'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('route,raw', [
    ('/externalcontact/add_moment_task', b'not-json'),
    ('/externalcontact/add_moment_task', b'{"errcode":false,"jobid":"bad"}'),
    ('/externalcontact/add_moment_task', b'{"errcode":0,"jobid":"'+b'a'*65+b'"}'),
    ('/externalcontact/add_moment_task', b'{"errcode":0,"jobid":"bad jobid PRIVATE"}'),
    ('/media/upload_attachment', b'{"errcode":0,"type":"image"}')])
def test_malformed_write_receipts_unknown_and_never_retried(route, raw):
    calls, claims = [], []
    def handler(r):
        if r.url.path.endswith('/gettoken'):
            return httpx.Response(200, json={'errcode': 0, 'access_token': 'fixture-token', 'expires_in': 7200})
        calls.append(r.url.path)
        return httpx.Response(200, content=raw)
    c = WeComMomentsClient(Credentials('ww1234567890', 1000002, 'fixture-secret'), transport=httpx.MockTransport(handler))
    with pytest.raises(MomentWriteError) as error:
        if 'upload' in route:
            c.upload({'media_type': 'images', 'format': 'PNG', 'sha256': 'a'*64}, b'fixture', lambda: claims.append(True))
        else:
            c.create({}, lambda: claims.append(True))
    assert error.value.uncertain and len(calls) == len(claims) == 1
    assert 'PRIVATE' not in str(error.value)
    c.close()


def test_actual_small_mp4_is_probed_frozen_and_uploaded_to_mock_only(pilot):
    import hashlib,shutil,subprocess
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('Existing ffmpeg/ffprobe required; never auto-install')
    path=pilot['root']/'actual-fixture.mp4'
    subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','color=c=navy:s=96x160:r=2',
                    '-t','1','-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True,timeout=10,capture_output=True)
    raw=path.read_bytes();assert len(raw)<100_000
    aid='asset_'+'d'*32
    store(LocalMediaAsset(id=aid,label='self-generated video fixture',media_type='video',
        manifest_json=json.dumps([{'path':str(path),'name':path.name,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}])))
    async def run():
        async with client() as h:
            d,_=await prepare(h,media_type='video',asset_ids=[aid])
            detail=(await h.get(ROOT+'/'+d['id'])).json()
            assert detail['media'][0]['format']=='MP4' and 0<detail['media'][0]['duration_ms']<=1000
            await approve(h,d)
            assert (await submit(h,d)).json()['status']=='creating' and pilot['uploads']==1
    asyncio.run(run())


@pytest.mark.parametrize('size',[5,10_000_001])
def test_media_byte_limits_fail_closed(pilot,size):
    import hashlib
    from fastapi import HTTPException
    from app.wecom_moments_media import media_bytes
    raw=b'x'*size;path=pilot['root']/'sized.mp4';path.write_bytes(raw)
    item={'path':str(path),'size':size,'sha256':hashlib.sha256(raw).hexdigest()}
    with pytest.raises(HTTPException) as error:media_bytes(item,pilot['root'])
    assert error.value.status_code==409 and pilot['uploads']==0


def test_expired_token_api_response_to_create_is_definitive_no_auto_retry():
    calls=[]
    def handle(r):
        calls.append(r.url.path)
        return httpx.Response(200,json={'errcode':0,'access_token':'fixture-token','expires_in':7200} if r.url.path.endswith('/gettoken') else {'errcode':40014,'errmsg':'PRIVATE'})
    c=WeComMomentsClient(Credentials('ww1234567890',1000002,'fixture-secret'),transport=httpx.MockTransport(handle))
    with pytest.raises(MomentWriteError) as error:c.create({},lambda:None)
    assert error.value.api_code==40014 and not error.value.uncertain
    assert len(calls)==2  # One token GET, one task POST; no token-refresh/resubmit.
    c.close()


def test_restart_marks_read_receipts_interrupted_without_replaying(pilot):
    import hashlib
    key='wecom-moment-fixture-interrupted-preflight'
    hashed=hashlib.sha256(('wecom-moments:preflight\0'+key).encode()).hexdigest()
    with db.get_session() as session:
        session.add(AppSetting(key=flow.PREFIX+'preflight:'+hashed,value=json.dumps({'status':'running','external_write_triggered':False})))
        session.add(AppSetting(key=flow.PREFIX+'query:fixture',value=json.dumps({'status':'running','task_id':'fixture','external_write_triggered':False})))
        session.commit()
    assert flow.recover()==2 and not pilot['calls']
    assert flow.recover()==0
    async def run():
        async with client() as h:
            r=await h.get(ROOT+'/receipts/preflight/'+key)
            assert r.status_code==200 and r.json()['status']=='interrupted'
            r=await h.post(ROOT+'/preflight',json={},headers={'Idempotency-Key':key})
            assert r.json()['status']=='interrupted' and not pilot['calls']
    asyncio.run(run())
