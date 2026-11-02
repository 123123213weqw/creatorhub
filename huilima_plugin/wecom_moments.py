"""Manual enterprise Moments in the existing Hub, not a new console/worker.

Drafts are local. Only a local owner can approve and submit exactly once.
Explicit enterprise customer tags are mandatory: never fall back to all
customers, departments, a chatbot, ordinary chats, or a client Hook.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import Field, StrictBool
from sqlmodel import select, or_

from .business_models import WeComMomentTask
from .business_workflows import Strict, audit, digest, encode, now, once
from .hub_adapter import get_session
from .hub_adapter import utc_iso
from .hub_adapter import _identity
from .wecom_moments_client import WeComMomentsClient
from .wecom_moments_media import freeze_assets, media_bytes, media_digest_items
from .wecom_readonly import WeComError, load_credentials
from .wecom_workflows import _owner, _read, _write

router = APIRouter(prefix='/api/integrations/wecom/moments')
PREFIX = 'wecom:moments:v1:'
BINDING = PREFIX + 'binding'
APPROVAL_TTL = timedelta(minutes=30)
BINDING_TTL = timedelta(hours=1)
QUERY_GAP = timedelta(seconds=60)


def factory():
    return WeComMomentsClient(load_credentials())


def upload_root():
    from .hub_adapter import UPLOAD_DIR
    return UPLOAD_DIR


def close_client(client):
    # A transport cleanup error must never discard an already received job ID.
    if client:
        try:
            client.close()
        except Exception:
            pass


def identity(request, scope, body):
    value = _identity(request, 'wecom-moments:' + scope, body)
    if not value:
        raise HTTPException(422, '需要稳定的Idempotency-Key；超时后沿用原编号核对')
    return value


def task(session, task_id):
    row = session.get(WeComMomentTask, task_id)
    if not row:
        raise HTTPException(404, '朋友圈草稿不存在')
    return row


def intent(row):
    return {'binding_id': row.binding_id, 'sender_user_id': row.sender_user_id,
        'customer_tag_ids': json.loads(row.customer_tag_ids_json),
        'customer_scope_note': row.customer_scope_note, 'text': row.text,
        'media_type': row.media_type, 'media': media_digest_items(json.loads(row.media_manifest_json)),
        'demo_only': row.demo_only}


def verify_intact(row):
    if digest(intent(row)) != row.digest:
        raise HTTPException(409, '内容、执行成员或客户范围已变化，请重新建立并审核草稿')
    for item in json.loads(row.media_manifest_json):
        media_bytes(item, upload_root())


def public(row, *, detail=False):
    result = {k: getattr(row, k) for k in (
        'id', 'label', 'digest', 'binding_id', 'status', 'phase', 'media_type', 'demo_only',
        'task_creation_attempts', 'media_upload_attempts', 'creation_job_status',
        'creation_scope_rejected', 'member_task_observed', 'member_published',
        'last_error_code', 'last_api_code', 'last_query_error', 'last_query_api_code')}
    result.update({k: utc_iso(getattr(row, k)) for k in (
        'created_at', 'updated_at', 'approved_at', 'submitted_at', 'last_query_at',
        'execution_checked_at')})
    result.update(media_count=len(json.loads(row.media_manifest_json)),
        customer_tag_count=len(json.loads(row.customer_tag_ids_json)),
        job_known=bool(row.job_id), moment_known=bool(row.moment_id),
        requires_member_confirmation=True, automatic_submit_enabled=False,
        automatic_polling_enabled=False, external_write_triggered=False,
        platform_refresh_triggered=False, chat_content_read=False,
        read_count=None, reply_count=None, sales_count=None,
        evidence_scope='task creation != member publication != customer read or sale')
    if detail:
        result.update(text=row.text, sender_user_id=row.sender_user_id,
            customer_tag_ids=json.loads(row.customer_tag_ids_json),
            customer_scope_note=row.customer_scope_note, review_note=row.review_note,
            job_id=row.job_id, moment_id=row.moment_id,
            media=[{**{k: v for k, v in item.items() if k != 'path'},
                    'preview_url': f'/api/integrations/wecom/moments/{row.id}/media/{i}'}
                   for i, item in enumerate(json.loads(row.media_manifest_json))])
    return result


def binding_view(value):
    if value is None:
        return {'binding_available': False, 'external_write_triggered': False}
    return {**value, 'binding_available': True,
        'binding_fresh': datetime.fromisoformat(value['verified_at']) + BINDING_TTL >= now(),
        'write_permission_verified': False, 'external_write_triggered': False}


@router.get('/binding')
def binding_status(request: Request):
    _owner(request)
    with get_session() as session:
        return binding_view(_read(session, BINDING))


@router.post('/preflight')
def preflight(request: Request):
    _owner(request)
    key, _ = identity(request, 'preflight', {})
    receipt = PREFIX + 'preflight:' + key
    stamp = now()
    with get_session() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        old = _read(session, receipt)
        if old:
            return {**old, 'replayed': True}
        gate = _read(session, PREFIX + 'preflight-gate')
        if gate and datetime.fromisoformat(gate['next_allowed']) > stamp:
            raise HTTPException(429, '企微成员核对至少间隔60秒')
        _write(session, PREFIX + 'preflight-gate', {'next_allowed': (stamp + QUERY_GAP).isoformat()})
        _write(session, receipt, {'status': 'running', 'external_write_triggered': False})
        session.commit()
    client = None
    try:
        client = factory()
        bound = client.binding()
        if (not re.fullmatch(r'[a-f0-9]{64}', bound['binding_id'])
                or not isinstance(bound['sender_user_id'], str) or not bound['sender_user_id']):
            raise ValueError()
        result = {'status': 'ready', **bound, 'verified_at': now().isoformat(),
            'write_permission_verified': False, 'external_write_triggered': False}
    except WeComError as error:
        result = {'status': 'failed', 'error_code': error.code, 'api_code': error.api_code,
                  'external_write_triggered': False}
    except Exception:
        result = {'status': 'failed', 'error_code': 'wecom_moment_preflight_unavailable',
                  'external_write_triggered': False}
    finally:
        close_client(client)
    with get_session() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        if result['status'] == 'ready':
            _write(session, BINDING, {k: result[k] for k in ('binding_id', 'sender_user_id', 'verified_at')})
        _write(session, receipt, result)
        session.commit()
    return result


class DraftIn(Strict):
    label: str = Field(min_length=1, max_length=80)
    binding_id: str = Field(pattern='^[a-f0-9]{64}$')
    text: str = Field(default='', max_length=2000)
    media_type: str = Field(default='text', pattern='^(text|images|video)$')
    asset_ids: list[str] = Field(default_factory=list, max_length=9)
    customer_tag_ids: list[str] = Field(min_length=1, max_length=10)
    customer_scope_note: str = Field(min_length=1, max_length=500)
    demo_only: StrictBool = False


@router.post('/drafts')
def create_draft(body: DraftIn, request: Request):
    _owner(request)
    if (len(body.text.encode('utf-8')) > 4000
            or (body.media_type == 'text' and not body.text.strip())
            or not body.label.strip() or not body.customer_scope_note.strip()
            or len(set(body.customer_tag_ids)) != len(body.customer_tag_ids)
            or any(not re.fullmatch(r'et[A-Za-z0-9_-]{3,158}', x) for x in body.customer_tag_ids)
            or any(not re.fullmatch(r'asset_[a-f0-9]{32}', x) for x in body.asset_ids)):
        raise HTTPException(422, '需有效文案、已登记素材及明确的企业客户标签，不允许空范围或全部客户')
    def create(session):
        bound = _read(session, BINDING)
        if (not bound or bound['binding_id'] != body.binding_id
                or datetime.fromisoformat(bound['verified_at']) + BINDING_TTL < now()):
            raise HTTPException(409, '请先核对当前应用的唯一测试成员，再创建草稿')
        media, asset_demo = freeze_assets(session, body.asset_ids, body.media_type, upload_root())
        row = WeComMomentTask(id='wecom_moment_' + uuid.uuid4().hex,
            label=body.label.strip(), binding_id=body.binding_id, sender_user_id=bound['sender_user_id'],
            text=body.text, media_type=body.media_type, media_manifest_json=encode(media),
            customer_tag_ids_json=encode(sorted(body.customer_tag_ids)),
            customer_scope_note=body.customer_scope_note.strip(), demo_only=body.demo_only or asset_demo, digest='')
        row.digest = digest(intent(row))
        session.add(row)
        audit(session, 'local_owner', 'wecom_moment_drafted', row.id,
              {'digest': row.digest, 'media_count': len(media), 'customer_tag_count': len(body.customer_tag_ids)})
        session.flush()
        return public(row)
    return once(request, 'wecom-moment-draft', body, create)


@router.get('/receipts/{operation}/{key}')
def receipt(operation: str, key: str, request: Request):
    _owner(request)
    from .hub_adapter import TaskSubmission
    if operation == 'preflight':
        from .wecom_workflows import _digest
        import hashlib
        _digest(key)  # Validate the caller key; receipt identity also includes scope.
        scoped_key = hashlib.sha256(('wecom-moments:preflight\0' + key).encode()).hexdigest()
        with get_session() as session:
            result = _read(session, PREFIX + 'preflight:' + scoped_key)
            if not result:
                raise HTTPException(404, '未找到原请求回执，不能据此认定操作没有发生')
            return {**result, 'replayed': True}
    if operation != 'draft':
        raise HTTPException(422, '不支持的回执类型')
    # Drafts are never deleted, so the original local receipt is sufficient.
    from .wecom_workflows import _digest
    _digest(key)
    import hashlib
    value = hashlib.sha256(('business:local_owner:wecom-moment-draft\0' + key).encode()).hexdigest()
    with get_session() as session:
        saved = session.get(TaskSubmission, value)
        if not saved:
            raise HTTPException(404, '未找到原草稿回执，请先核对列表，不要重新编号')
        result = json.loads(saved.response_json)
        return {**public(task(session, result['id'])), 'replayed': True}


@router.get('')
def list_tasks(request: Request):
    _owner(request)
    with get_session() as session:
        rows = session.exec(select(WeComMomentTask).order_by(WeComMomentTask.created_at.desc()).limit(201)).all()
        return {'items': [public(row) for row in rows[:200]], 'more_may_exist': len(rows) > 200,
                'external_write_triggered': False, 'platform_refresh_triggered': False}


@router.get('/{task_id}')
def show_task(task_id: str, request: Request):
    _owner(request)
    with get_session() as session:
        return public(task(session, task_id), detail=True)


@router.get('/{task_id}/media/{index}')
def show_media(task_id: str, index: int, request: Request):
    _owner(request)
    with get_session() as session:
        row = task(session, task_id)
        verify_intact(row)
        media = json.loads(row.media_manifest_json)
        if not 0 <= index < len(media):
            raise HTTPException(404, '素材不存在')
        item = media[index]
        mime = 'video/mp4' if item['media_type'] == 'video' else 'image/png' if item['format'] == 'PNG' else 'image/jpeg'
        return Response(media_bytes(item, upload_root()), media_type=mime)


class ReviewIn(Strict):
    digest: str = Field(pattern='^[a-f0-9]{64}$')
    review_note: str = Field(min_length=1, max_length=500)
    acknowledge_content: StrictBool
    acknowledge_customer_tags: StrictBool
    acknowledge_member_notification: StrictBool


@router.post('/{task_id}/review')
def review(task_id: str, body: ReviewIn, request: Request):
    _owner(request)
    if not (body.acknowledge_content and body.acknowledge_customer_tags
            and body.acknowledge_member_notification and body.review_note.strip()):
        raise HTTPException(422, '必须核对完整内容、标签内客户范围及向成员创建发表任务的影响')
    def update(session):
        row = task(session, task_id)
        verify_intact(row)
        if row.demo_only or body.digest != row.digest:
            raise HTTPException(409, '演示素材不可外发，或审核内容已变化')
        if row.status == 'approved' and row.approved_digest == body.digest:
            return public(row)  # Do not silently extend the approval deadline.
        if row.status != 'draft':
            raise HTTPException(409, '当前任务不能重新审核或重置，请核对原任务')
        row.approved_at = now()
        row.approved_digest = row.digest
        row.review_note = body.review_note.strip()
        row.approved_by = 'local_owner'
        row.status, row.phase, row.updated_at = 'approved', 'local_approved', now()
        session.add(row)
        audit(session, 'local_owner', 'wecom_moment_reviewed', row.id, {'digest': row.digest})
        return public(row)
    return once(request, 'wecom-moment-review:' + task_id, body, update)


class SubmitIn(Strict):
    digest: str = Field(pattern='^[a-f0-9]{64}$')
    acknowledge_external_writes: StrictBool


def mark_attempt(task_id, kind):
    with get_session() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = task(session, task_id)
        if row.status != 'submitting' or row.task_creation_attempts:
            raise HTTPException(409, '任务已尝试提交或状态变化，不重复创建')
        if kind == 'task':
            row.task_creation_attempts = 1
            row.submitted_at, row.phase = now(), 'task_post_claimed'
        else:
            row.media_upload_attempts += 1
            row.phase = 'media_post_claimed'
        session.add(row)
        audit(session, 'local_owner', 'wecom_moment_' + kind + '_claimed', task_id)
        session.commit()


@router.post('/{task_id}/submit')
def submit(task_id: str, body: SubmitIn, request: Request):
    _owner(request)
    key, body_hash = identity(request, 'submit:' + task_id, body)
    if not body.acknowledge_external_writes:
        raise HTTPException(422, '创建发表任务会通知成员，上传素材会传至腾讯，必须明确确认')
    with get_session() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = task(session, task_id)
        if row.submit_key_hash:
            if row.submit_key_hash == key and row.submit_body_hash != body_hash:
                raise HTTPException(409, '原提交编号内容冲突')
            return {**public(row), 'replayed': True}  # Never issue another POST for this task.
        verify_intact(row)
        if (row.status != 'approved' or row.demo_only or body.digest != row.digest
                or row.approved_digest != row.digest or not row.approved_at
                or not row.approved_at <= now() <= row.approved_at + APPROVAL_TTL):
            raise HTTPException(409, '需对完整内容和范围人工审核，审核30分钟后失效')
        gate = _read(session, PREFIX + 'create-gate')
        if gate and datetime.fromisoformat(gate['next_allowed']) > now():
            raise HTTPException(429, '朋友圈任务创建至少间隔60秒')
        _write(session, PREFIX + 'create-gate', {'next_allowed': (now() + QUERY_GAP).isoformat()})
        row.submit_key_hash, row.submit_body_hash = key, body_hash
        row.status, row.phase, row.updated_at = 'submitting', 'checking_scope', now()
        session.add(row)
        audit(session, 'local_owner', 'wecom_moment_submit_started', task_id, {'digest': row.digest})
        session.commit()
        session.refresh(row)
        frozen = WeComMomentTask(**row.model_dump())
    client, job, failure, code, uploads = None, '', '', None, []
    try:
        client = factory()
        client.assert_binding(frozen.binding_id, frozen.sender_user_id)
        for item in json.loads(frozen.media_manifest_json):
            # Upload immutable bytes checked against exactly the reviewed digest.
            content = media_bytes(item, upload_root())
            uploaded = client.upload(item, content, lambda: mark_attempt(task_id, 'media'))
            uploads.append(uploaded)
            with get_session() as session:
                current = task(session, task_id)
                current.uploads_json = encode(uploads)
                session.add(current)
                session.commit()
        payload = {'visible_range': {
            'sender_list': {'user_list': [frozen.sender_user_id]},
            'external_contact_list': {'tag_list': json.loads(frozen.customer_tag_ids_json)}}}
        if frozen.text:
            payload['text'] = {'content': frozen.text}
        if uploads:
            kind = 'video' if frozen.media_type == 'video' else 'image'
            payload['attachments'] = [{'msgtype': kind, kind: {'media_id': x['media_id']}} for x in uploads]
        job = client.create(payload, lambda: mark_attempt(task_id, 'task'))
    except WeComError as error:
        failure, code = error.code, error.api_code
    except Exception:
        failure = 'wecom_moment_submission_unknown'
    finally:
        close_client(client)
    with get_session() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        current = task(session, task_id)
        if current.status != 'submitting':
            raise HTTPException(409, '任务状态变化，请核对原回执，不要重复提交')
        current.last_error_code, current.last_api_code = failure, code
        if job:
            current.job_id, current.job_received_at = job, now()
            current.status, current.phase = 'creating', 'job_received_not_published'
        elif not current.task_creation_attempts:
            current.status, current.phase = 'failed_before_task', 'no_task_post_attempted'
        else:
            current.status = 'rejected' if failure == 'wecom_moment_api_rejected' else 'unknown'
            current.phase = 'task_rejected' if current.status == 'rejected' else 'task_outcome_requires_manual_check'
        current.updated_at = now()
        session.add(current)
        audit(session, 'local_owner', 'wecom_moment_' + current.status, task_id, {'error_code': failure})
        session.commit()
        return {**public(current), 'external_write_triggered': bool(current.media_upload_attempts or current.task_creation_attempts)}


class QueryIn(Strict):
    acknowledge_platform_read: StrictBool


@router.post('/{task_id}/refresh')
def refresh(task_id: str, body: QueryIn, request: Request):
    _owner(request)
    key, _ = identity(request, 'query:' + task_id, body)
    if not body.acknowledge_platform_read:
        raise HTTPException(422, '需确认只读查询此任务的创建结果和测试成员发表状态')
    receipt_key = PREFIX + 'query:' + key
    with get_session() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        old = _read(session, receipt_key)
        if old:
            return {**old, 'replayed': True, 'platform_refresh_triggered': False}
        row = task(session, task_id)
        if row.status not in {'creating', 'awaiting_member', 'published', 'scope_rejected'}:
            raise HTTPException(409, '尚无可查询的创建回执；未知提交须先在企微后台人工核对')
        if row.query_nonce or (row.last_query_at and row.last_query_at + QUERY_GAP > now()):
            raise HTTPException(429, '该任务正在查询或未达到60秒间隔')
        row.query_nonce = uuid.uuid4().hex
        row.query_started_at, row.last_query_at = now(), now()
        session.add(row)
        _write(session, receipt_key, {'status': 'running', 'task_id': task_id,
                                     'platform_refresh_triggered': False, 'external_write_triggered': False})
        session.commit()
        session.refresh(row)
        frozen = WeComMomentTask(**row.model_dump())
    client, creation, member, failure, code = None, None, None, '', None
    try:
        client = factory()
        client.assert_binding(frozen.binding_id, frozen.sender_user_id)
        moment = frozen.moment_id
        if not moment:
            if not frozen.job_id or not frozen.job_received_at or frozen.job_received_at + timedelta(hours=24) <= now():
                raise WeComError('wecom_moment_job_expired_requires_manual_check')
            creation = client.creation_result(frozen.job_id)
            moment = creation.get('moment_id', '')
        if moment and not (creation or {}).get('scope_rejected') and not frozen.creation_scope_rejected:
            member = client.member_result(moment, frozen.sender_user_id)
    except WeComError as error:
        failure, code = error.code, error.api_code
    except Exception:
        failure = 'wecom_moment_read_unavailable'
    finally:
        close_client(client)
    with get_session() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = task(session, task_id)
        if row.query_nonce != frozen.query_nonce:
            raise HTTPException(409, '查询回执已变化，请查看当前状态')
        row.query_nonce = ''
        row.last_query_error, row.last_query_api_code = failure, code
        if creation:
            row.creation_job_status = creation['status']
            if creation.get('rejected'):
                row.status, row.phase = 'rejected', 'async_creation_rejected'
                row.last_error_code, row.last_api_code = 'wecom_moment_async_rejected', creation['api_code']
            elif creation.get('scope_rejected'):
                row.moment_id = creation.get('moment_id', '')
                row.creation_scope_rejected = True
                row.status, row.phase = 'scope_rejected', 'invalid_member_or_customer_tags_no_retry'
            elif creation.get('moment_id'):
                row.moment_id = creation['moment_id']
                row.status, row.phase = 'awaiting_member', 'task_created_not_published'
        if member:
            # A later empty/pending result must not revoke prior published evidence.
            if row.status == 'published' and member['member_published'] is not True:
                row.last_query_error = 'wecom_moment_execution_result_changed_requires_check'
            else:
                row.member_task_observed, row.member_published = member['member_task_observed'], member['member_published']
                row.execution_checked_at = now()
                if member['member_published'] is True:
                    row.status, row.phase = 'published', 'member_publication_verified_not_customer_read'
        row.updated_at = now()
        session.add(row)
        audit(session, 'local_owner', 'wecom_moment_checked', task_id, {'error_code': failure, 'status': row.status})
        result = {**public(row), 'platform_refresh_triggered': True}
        _write(session, receipt_key, result)
        session.commit()
        return result


class CancelIn(Strict):
    reason: str = Field(min_length=1, max_length=500)


@router.post('/{task_id}/cancel')
def cancel(task_id: str, body: CancelIn, request: Request):
    _owner(request)
    def update(session):
        row = task(session, task_id)
        if row.status not in {'draft', 'approved', 'failed_before_task', 'rejected'} or not body.reason.strip():
            raise HTTPException(409, '只能取消本地草稿或已明确拒绝的任务；不会撤回企微任务')
        row.status, row.phase, row.updated_at = 'canceled', 'local_only_not_remote_recall', now()
        session.add(row)
        audit(session, 'local_owner', 'wecom_moment_canceled_locally', task_id, {'reason': body.reason.strip()})
        return public(row)
    return once(request, 'wecom-moment-cancel:' + task_id, body, update)


def recover():
    """Restart never recreates a task or polls the platform."""
    with get_session() as session:
        rows = session.exec(select(WeComMomentTask).where(or_(
            WeComMomentTask.status == 'submitting', WeComMomentTask.query_nonce != ''))).all()
        changed = 0
        for row in rows:
            if row.status == 'submitting':
                row.status = 'unknown' if row.task_creation_attempts else 'failed_before_task'
                row.phase = 'restart_requires_original_task_check'
                row.last_error_code = 'wecom_moment_process_interrupted'
                changed += 1
            if row.query_nonce:
                row.query_nonce = ''
                row.last_query_error = 'wecom_moment_query_interrupted'
                changed += 1
            session.add(row)
        from .hub_adapter import AppSetting
        receipts = session.exec(select(AppSetting).where(or_(
            AppSetting.key.startswith(PREFIX + 'preflight:'),
            AppSetting.key.startswith(PREFIX + 'query:')))).all()
        for receipt_row in receipts:
            value = json.loads(receipt_row.value)
            if value.get('status') == 'running':
                value.update(status='interrupted', error_code='wecom_moment_read_interrupted',
                             external_write_triggered=False, platform_refresh_triggered=False)
                receipt_row.value = encode(value)
                session.add(receipt_row)
                changed += 1
        session.commit()
        return changed
