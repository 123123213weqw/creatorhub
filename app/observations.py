"""Offline public-metric/comment snapshots with provenance, nulls and dedup.

Never fetches URLs or runs a platform client. Imported comments become review
candidates, not approved CRM records. No names/quotes are exposed by the CLI.
"""
from __future__ import annotations
from datetime import datetime,timedelta
import hashlib,json
from urllib.parse import urlsplit
from fastapi import APIRouter,HTTPException,Request
from pydantic import Field,StrictBool,StrictInt,field_validator,model_validator
from sqlmodel import select
from .db import get_session
from .business_models import ObservationSnapshot,OfflineCommentCursor
from .business_workflows import Strict,principal,once,now,when,uid,encode,digest,audit,row_dict
from .models import DouyinAccount,AccountWork,LeadCandidate
from .operations_workflows import SIGNALS,NEGATIVE,digest as lead_digest

router=APIRouter(prefix='/api/operations')
METRICS={'views','likes','comments','shares','favorites'}
HOSTS={'douyin':{'www.douyin.com','v.douyin.com'},
       'xhs':{'www.xiaohongshu.com','xiaohongshu.com','xhslink.com'},
       'shipinhao':{'channels.weixin.qq.com','mp.weixin.qq.com'},
       'kuaishou':{'www.kuaishou.com','v.kuaishou.com'},
       'tiktok':{'www.tiktok.com','vm.tiktok.com'}}


class MetricIn(Strict):
    value:StrictInt|None=Field(default=None,ge=0,le=10**12)
    reason:str=Field(default='',max_length=200)

    @model_validator(mode='after')
    def explain(self):
        if self.value is None and not self.reason.strip():raise ValueError('unknown metric needs reason')
        if self.value is not None and self.reason:raise ValueError('known metric cannot have an unavailable reason')
        return self


class CommentIn(Strict):
    comment_id:str=Field(min_length=1,max_length=160)
    text:str=Field(min_length=1,max_length=2000)
    nickname:str=Field(default='',max_length=100)
    author_ref:str=Field(default='',max_length=160)
    commented_at:str=Field(max_length=80)

    @field_validator('comment_id','author_ref')
    @classmethod
    def identifier(cls,v):
        if any(ord(c)<33 or ord(c)==127 for c in v):raise ValueError('invalid identifier')
        return v


class ImportIn(Strict):
    account_id:StrictInt=Field(gt=0)
    work_id:StrictInt=Field(default=0,ge=0)
    item_id:str=Field(min_length=1,max_length=160)
    source_url:str=Field(min_length=8,max_length=2000)
    source_kind:str=Field(pattern='^(manual_backend|authorized_export)$')
    source_note:str=Field(min_length=1,max_length=500)
    observed_at:str=Field(max_length=80)
    metrics:dict[str,MetricIn]
    comments:list[CommentIn]=Field(default_factory=list,max_length=100)
    comments_complete:StrictBool=False
    is_demo:StrictBool=False
    acknowledge_authorized_source:StrictBool

    @model_validator(mode='after')
    def contract(self):
        if self.acknowledge_authorized_source is not True:raise ValueError('explicit source approval required')
        if set(self.metrics)!=METRICS:raise ValueError('provide all metrics with unknown reasons')
        if len({c.comment_id for c in self.comments})!=len(self.comments):raise ValueError('duplicate comment IDs')
        if not self.source_note.strip():raise ValueError('source note required')
        return self


def summary(row):
    return {**row_dict(row,{'comments_json','source_url','source_note','metrics_json'}),
            'metrics':json.loads(row.metrics_json),'imported_comment_count':len(json.loads(row.comments_json)),
            'source_url_available':True,'platform_refresh_triggered':False}


def create(s,body,actor):
    account=s.get(DouyinAccount,body.account_id)
    if not account or account.platform not in HOSTS:raise HTTPException(422,'observation_account_invalid')
    if body.work_id:
        work=s.get(AccountWork,body.work_id)
        if not work or (work.account_id,work.platform,work.item_id)!=(account.id,account.platform,body.item_id):
            raise HTTPException(409,'observation_work_binding_mismatch')
    try:
        url=urlsplit(body.source_url)
        if (url.scheme!='https' or url.hostname not in HOSTS[account.platform] or url.username or url.password
                or url.port not in (None,443) or url.fragment or url.query
                or any(ord(c)<33 or ord(c)==127 for c in body.source_url)):raise ValueError()
    except ValueError:raise HTTPException(422,'observation_canonical_platform_url_required') from None
    stamp=when(body.observed_at)
    if stamp>now()+timedelta(minutes=5):raise HTTPException(422,'observation_future_time')
    for comment in body.comments:
        if when(comment.commented_at)>stamp+timedelta(minutes=5):raise HTTPException(422,'comment_after_observation')
    payload=body.model_dump();content_digest=digest(payload)
    old=s.exec(select(ObservationSnapshot).where(ObservationSnapshot.content_digest==content_digest)).first()
    if old:return {'snapshot':summary(old),'created':False,'leads_created':0,'leads_updated':0,'external_write_triggered':False}
    row=ObservationSnapshot(id=uid('observation'),account_id=account.id,platform=account.platform,
        work_id=body.work_id,item_id=body.item_id,source_url=body.source_url,source_kind=body.source_kind,
        source_note=body.source_note,observed_at=stamp,imported_by=actor,metrics_json=encode(payload['metrics']),
        comments_json=encode(payload['comments']),comments_complete=body.comments_complete,
        is_demo=body.is_demo,content_digest=content_digest)
    s.add(row)
    added=updated=0
    for comment in body.comments:
        tags=[] if any(v in comment.text for v in NEGATIVE) else [k for k,words in SIGNALS.items() if any(v in comment.text for v in words)]
        source=f'{account.platform}:{account.id}:{body.item_id}:{comment.comment_id}'
        key=hashlib.sha256((('demo:' if body.is_demo else '')+source).encode()).hexdigest()
        cursor=s.get(OfflineCommentCursor,key)
        if cursor and cursor.observed_at>stamp:continue
        if cursor and cursor.observed_at==stamp:
            previous=s.get(ObservationSnapshot,cursor.snapshot_id)
            previous_comments=json.loads(previous.comments_json) if previous else []
            old_comment=next((x for x in previous_comments if x['comment_id']==comment.comment_id),None)
            if old_comment!=comment.model_dump():raise HTTPException(409,'observation_same_time_comment_conflict')
        cursor=cursor or OfflineCommentCursor(source_key=key,observed_at=stamp,snapshot_id=row.id)
        cursor.observed_at=stamp;cursor.snapshot_id=row.id;s.add(cursor)
        identity='uid:'+comment.author_ref if comment.author_ref else 'comment:'+key
        external=('demo_' if body.is_demo else '')+'hlm_'+hashlib.sha256(f'{account.platform}:{identity}'.encode()).hexdigest()
        lead=s.exec(select(LeadCandidate).where(LeadCandidate.source_key==key)).first()
        values={'external_key':external,'platform':account.platform,'account_id':account.id,'work_id':body.work_id,
                'item_id':body.item_id,'comment_id':comment.comment_id,'source_nickname':comment.nickname,
                'comment_text':comment.text,'signal_tags':encode(tags),'is_demo':body.is_demo}
        if lead is None:
            if not tags:continue
            lead=LeadCandidate(source_key=key,**values);lead.digest=lead_digest(lead);s.add(lead);added+=1
        elif any(getattr(lead,k)!=v for k,v in values.items()):
            if lead.state=='recorded_mock':continue
            # A changed source invalidates earlier review and any queued CRM snapshot.
            for k,v in values.items():setattr(lead,k,v)
            lead.version+=1;lead.state='candidate';lead.approved_digest='';lead.reviewed_at=None
            lead.owner_label='';lead.review_note='';lead.updated_at=now();lead.digest=lead_digest(lead);s.add(lead);updated+=1
    audit(s,actor,'observation_imported',row.id,{'account_id':account.id,'is_demo':body.is_demo,'source_kind':body.source_kind})
    s.flush()
    return {'snapshot':summary(row),'created':True,'leads_created':added,'leads_updated':updated,
            'external_write_triggered':False,'human_lead_review_required':True}


@router.post('/observations')
def import_snapshot(body:ImportIn,request:Request):
    p=principal(request,{'operator'})
    return once(request,'observation',body,lambda s:create(s,body,p['id']))


@router.get('/observations')
def snapshots(request:Request,account_id:int|None=None):
    principal(request,{'operator','sales','maintainer'})
    with get_session() as s:
        q=select(ObservationSnapshot)
        if account_id is not None:q=q.where(ObservationSnapshot.account_id==account_id)
        rows=s.exec(q.order_by(ObservationSnapshot.imported_at.desc()).limit(101)).all()
        return {'items':[summary(r) for r in rows[:100]],'more_may_exist':len(rows)>100,'platform_refresh_triggered':False}


@router.get('/observations/{oid}')
def detail(oid:str,request:Request):
    principal(request,{'operator','sales','maintainer'})
    with get_session() as s:
        r=s.get(ObservationSnapshot,oid)
        if not r:raise HTTPException(404,'observation_not_found')
        return summary(r)
