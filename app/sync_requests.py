"""Durable, deliberately sparse read queue inside the Hub, never a CLI worker."""
import asyncio
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import update
from sqlmodel import select

from .db import get_session
from .models import DouyinAccount, AccountWork, SyncRequest, SyncPacing
from .scheduling import utc_iso

# Business defaults, NOT promises about any platform's risk thresholds.
GLOBAL_GAP = timedelta(minutes=10)
ACCOUNT_GAP = timedelta(minutes=30)
MAX_ACTIVE = 20
MAX_ATTEMPTS = 3


def as_dict(row):
    keys = ("id", "platform", "account_id", "kind", "work_id", "status",
            "attempts", "blocked_reason", "error", "fetched", "added")
    return {**{k: getattr(row, k) for k in keys}, **{
        k: utc_iso(getattr(row, k)) for k in
        ("created_at", "started_at", "finished_at", "next_allowed_at")}}


def create_request(session, body):
    account = session.get(DouyinAccount, body.account_id)
    if not account or account.status != "active":
        raise HTTPException(422, "账号不存在或登录态已失效")
    if account.platform != "douyin":
        raise HTTPException(422, "首版受控同步仅开放抖音自有账号")
    if body.kind == "comments":
        work = session.get(AccountWork, body.work_id) if body.work_id else None
        if not work or work.account_id != account.id or work.platform != account.platform:
            raise HTTPException(422, "作品不属于指定账号")
    elif body.work_id is not None:
        raise HTTPException(422, "作品同步不接受 work_id")
    key = f"{account.id}:{body.kind}:{body.work_id or 0}"
    active = session.exec(select(SyncRequest).where(SyncRequest.active_key == key)).first()
    if active:
        if body.kind == "comments" and active.work_item_id != work.item_id:
            raise HTTPException(409, "原同步目标已变化，请先取消原任务")
        return {**as_dict(active), "deduplicated": True}
    if len(session.exec(select(SyncRequest.id).where(
            SyncRequest.status.in_(["pending", "running"]))).all()) >= MAX_ACTIVE:
        raise HTTPException(429, "受控同步队列已满，请等待现有任务")
    row = SyncRequest(platform=account.platform, account_id=account.id,
                      kind=body.kind, work_id=body.work_id,
                      work_item_id=work.item_id if body.kind == "comments" else "", active_key=key)
    session.add(row); session.flush()
    return as_dict(row)


def recover():
    """A partial read is safe to inspect, but never replayed as a catch-up burst."""
    with get_session() as session:
        rows = session.exec(select(SyncRequest).where(SyncRequest.status == "running")).all()
        for row in rows:
            row.status = "failed"; row.active_key = None
            row.error = "同步中断，可能已有部分本地快照；请查看后手动新建请求"
            row.finished_at = datetime.utcnow()
            session.add(row)
        session.commit()
        return len(rows)


async def process_one(handlers):
    """Claim at most one request; persistent gates apply even across restarts.

    Existing handlers retain the Hub account operation lock, risk preflight,
    proxy/auth checks and transport policy. No new scraping path is introduced.
    A SQLite writer lock serializes claim/cancel/creation across API processes.
    """
    if not handlers:
        return
    now = datetime.utcnow()
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        global_gate = session.get(SyncPacing, 0)
        rows = session.exec(select(SyncRequest).where(
            SyncRequest.status == "pending").order_by(SyncRequest.id)).all()
        selected = None
        for row in rows:
            account_gate = session.get(SyncPacing, row.account_id)
            due = max([now] + [v for v in (row.next_allowed_at,
                global_gate.next_allowed_at if global_gate else None,
                account_gate.next_allowed_at if account_gate else None) if v])
            if due > now:
                row.next_allowed_at = due; row.blocked_reason = "本机低频同步间隔（非平台安全阈值）"
                session.add(row)
                continue
            selected = row
            break
        if selected is None:
            session.commit(); return
        selected.status = "running"; selected.started_at = now
        selected.attempts += 1; selected.blocked_reason = ""; selected.next_allowed_at = None
        for aid, gap in ((0, GLOBAL_GAP), (selected.account_id, ACCOUNT_GAP)):
            gate = session.get(SyncPacing, aid) or SyncPacing(account_id=aid, next_allowed_at=now)
            gate.next_allowed_at = now + gap; session.add(gate)
        session.add(selected); session.commit()
        task_id, kind = selected.id, selected.kind
        account_id, work_id, item_id = selected.account_id, selected.work_id, selected.work_item_id
    try:
        # Revalidate comments binding immediately before the guarded adapter.
        with get_session() as session:
            account = session.get(DouyinAccount, account_id)
            work = session.get(AccountWork, work_id) if kind == "comments" else None
            if not account or account.status != "active" or account.platform != "douyin":
                raise HTTPException(422, "账号不可用")
            if kind == "comments" and (not work or work.account_id != account_id or work.platform != account.platform or work.item_id != item_id):
                raise HTTPException(422, "作品账号绑定已变化")
        result = await asyncio.wait_for(
            handlers[kind](account_id if kind == "works" else work_id), timeout=240)
        deferred = bool(result.get("skipped") or result.get("source") == "deferred")
        success = bool(result.get("ok")) and not deferred
        error = "" if success else ("已有读取正在执行或风控暂缓" if deferred else "同步失败，请在 Hub 日志中核对")
    except Exception:
        result, success, deferred = {}, False, False
        error = "同步失败，请在 Hub 检查账号、网络与风控状态；未自动重试"
    with get_session() as session:
        row = session.get(SyncRequest, task_id)
        if row is None or row.status != "running":
            return
        row.fetched = int(result.get("fetched") or 0); row.added = int(result.get("added") or 0)
        if deferred and row.attempts < MAX_ATTEMPTS:
            row.status = "pending"; row.blocked_reason = error
            row.next_allowed_at = datetime.utcnow() + ACCOUNT_GAP
        else:
            row.status = "done" if success else "failed"
            row.active_key = None; row.finished_at = datetime.utcnow(); row.error = error
        session.add(row); session.commit()
