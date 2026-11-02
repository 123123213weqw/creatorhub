"""Material IDs and offline comment→review→local CRM rehearsal inside ONE Hub."""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlmodel import select

from .db import get_session
from .models import (AccountWork, CommentRecord, DouyinAccount, LeadCandidate,
                     LocalMediaAsset, MockCustomerRecord, RiskAdminAudit)
from .publish_review import snapshot_media, file_hash
from .scheduling import utc_iso
from .submissions import submit_once
from .crm_adapter import DEFAULT_MAPPING, map_fields

router = APIRouter()
SIGNALS = {"price": ["多少钱", "价格", "费用", "收费", "报价"],
           "consult": ["想了解", "咨询", "怎么联系", "联系方式", "怎么报名", "报名", "想参加", "预约", "了解一下"]}
NEGATIVE = ["不要联系", "别联系", "不感兴趣", "不需要", "不用了"]


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetIn(StrictBody):
    label: str = Field(min_length=1,max_length=80)
    media_type: str = Field(pattern="^(images|video)$")
    media_paths: list[str] = Field(min_length=1,max_length=9)
    is_demo: StrictBool = False


class ProposeIn(StrictBody):
    account_id: StrictInt = Field(gt=0)
    work_id: StrictInt = Field(gt=0)


class ReviewIn(StrictBody):
    version: StrictInt = Field(gt=0)
    digest: str = Field(pattern="^[a-f0-9]{64}$")
    decision: str = Field(pattern="^(approve|reject)$")
    owner_label: str = Field(default="",max_length=80)
    note: str = Field(min_length=1,max_length=500)


class VersionIn(StrictBody):
    version: StrictInt = Field(gt=0)
    digest: str = Field(pattern="^[a-f0-9]{64}$")


def key_required(request):
    if not request.headers.get('Idempotency-Key'):
        raise HTTPException(422,'需要稳定的 Idempotency-Key')


def admin(request):
    from .main import _require_risk_admin
    return _require_risk_admin(request)


def asset_dict(row):
    manifest=json.loads(row.manifest_json)
    return {"id":row.id,"label":row.label,"media_type":row.media_type,"is_demo":row.is_demo,
            "media_count":len(manifest),"created_at":utc_iso(row.created_at),
            "media":[{k:m[k] for k in ('name','size','sha256')} for m in manifest]}


@router.post('/api/local-assets')
async def import_asset(body:AssetIn, request:Request):
    key_required(request)
    from .main import UPLOAD_DIR
    with get_session() as s:
        def create():
            manifest=snapshot_media(body.media_paths,body.media_type,UPLOAD_DIR)
            row=LocalMediaAsset(id='asset_'+uuid.uuid4().hex,label=body.label,media_type=body.media_type,
                manifest_json=json.dumps(manifest),is_demo=body.is_demo)
            s.add(row);s.flush();return asset_dict(row)
        result,_=submit_once(s,request=request,scope='asset',body=body,create=create)
        return result


@router.get('/api/local-assets')
async def list_assets():
    with get_session() as s:
        return [asset_dict(r) for r in s.exec(select(LocalMediaAsset).order_by(LocalMediaAsset.created_at.desc()).limit(100)).all()]


def resolve_asset_paths(s,ids,media_type):
    paths=[];demo=False
    for aid in ids:
        row=s.get(LocalMediaAsset,aid)
        if not row or row.media_type!=media_type:
            raise HTTPException(422,'素材编号不存在或类型不匹配')
        demo=demo or row.is_demo
        for m in json.loads(row.manifest_json):
            p=Path(m['path'])
            if p.is_symlink() or not p.is_file() or p.stat().st_size!=m['size'] or file_hash(p)!=m['sha256']:
                raise HTTPException(409,'素材副本已变化，请重新导入')
            paths.append(str(p))
    if not paths or len(paths)>9 or (media_type=='video' and len(paths)!=1):
        raise HTTPException(422,'素材总数超出草稿支持范围')
    return paths,demo


def digest(row):
    keys=('external_key','platform','account_id','work_id','item_id','comment_id','source_nickname','comment_text','signal_tags','is_demo')
    return hashlib.sha256(json.dumps({k:getattr(row,k) for k in keys},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def lead_dict(row,detail=False):
    keys=('id','platform','account_id','work_id','version','digest','state','owner_label','is_demo')
    d={k:getattr(row,k) for k in keys}
    d.update(signal_tags=json.loads(row.signal_tags),created_at=utc_iso(row.created_at),reviewed_at=utc_iso(row.reviewed_at))
    if detail:
        d.update(comment_id=row.comment_id,comment_text=row.comment_text,source_nickname=row.source_nickname,
                 review_note=row.review_note,external_key=row.external_key)
    return d


def propose(s,account_id,work_id):
    account=s.get(DouyinAccount,account_id);work=s.get(AccountWork,work_id)
    if not account or account.platform!='douyin' or not work or work.account_id!=account_id or work.platform!=account.platform:
        raise HTTPException(422,'账号与作品不匹配')
    # Same ownership predicate as existing own-work comments API; never search
    # another account's watches, private messages, or a remote platform here.
    rows=s.exec(select(CommentRecord).where(CommentRecord.platform==work.platform,
        CommentRecord.aweme_id==work.item_id,CommentRecord.watch_id==0).order_by(CommentRecord.id.desc()).limit(1000)).all()
    created=updated=matched=0
    seen=set()
    for c in rows:
        if c.comment_id in seen:continue
        seen.add(c.comment_id)
        tags=[] if any(x in c.text for x in NEGATIVE) else [k for k,words in SIGNALS.items() if any(w in c.text for w in words)]
        if tags:matched+=1
        sk=hashlib.sha256(f'{work.platform}:{account_id}:{work.item_id}:{c.comment_id}'.encode()).hexdigest()
        # Stable commenters dedupe across owned accounts; anonymous fallback
        # never merges unrelated users merely because their nickname matches.
        identity=('uid:'+c.user_sec_uid) if c.user_sec_uid else 'comment:'+sk
        ek='hlm_'+hashlib.sha256(f'{work.platform}:{identity}'.encode()).hexdigest()
        row=s.exec(select(LeadCandidate).where(LeadCandidate.source_key==sk)).first()
        values=dict(external_key=ek,platform=work.platform,account_id=account_id,work_id=work_id,item_id=work.item_id,
                    comment_id=c.comment_id,source_nickname=c.user_nickname,comment_text=c.text,signal_tags=json.dumps(tags))
        if row is None:
            if not tags:continue
            row=LeadCandidate(source_key=sk,**values);row.digest=digest(row);s.add(row);created+=1
        elif any(getattr(row,k)!=v for k,v in values.items()):
            if row.state=='recorded_mock':continue # frozen history, no silent CRM overwrite
            for k,v in values.items():setattr(row,k,v)
            row.version+=1;row.state='candidate';row.approved_digest='';row.reviewed_at=None
            row.owner_label='';row.review_note='';row.updated_at=datetime.utcnow();row.digest=digest(row);s.add(row);updated+=1
    s.flush()
    return {'account_id':account_id,'work_id':work_id,'scanned':len(rows),'matched':matched,'created':created,'updated':updated,
            'scan_limit':1000,'more_may_exist':len(rows)>=1000,'platform_refresh_triggered':False,'analysis':'local_explainable_rules','customer_identity_verified':False}


@router.post('/api/leads/propose')
async def propose_leads(body:ProposeIn,request:Request):
    key_required(request)
    with get_session() as s:
        result,_=submit_once(s,request=request,scope='lead-scan',body=body,create=lambda:propose(s,body.account_id,body.work_id))
        return result


@router.get('/api/leads')
async def list_leads(account_id:int|None=None,state:str='',page:int=1,page_size:int=20):
    if state and state not in {'candidate','approved','rejected','recorded_mock'}:raise HTTPException(422,'状态不正确')
    page=max(1,page);page_size=max(1,min(100,page_size))
    with get_session() as s:
        q=select(LeadCandidate)
        if account_id:q=q.where(LeadCandidate.account_id==account_id)
        if state:q=q.where(LeadCandidate.state==state)
        rows=s.exec(q.order_by(LeadCandidate.id.desc()).limit(1001)).all()
        return {'items':[lead_dict(r) for r in rows[(page-1)*page_size:page*page_size]],
                'total_in_snapshot':min(len(rows),1000),'more_may_exist':len(rows)>1000,'page':page,'page_size':page_size}


@router.get('/api/leads/{lid:int}')
async def show_lead(lid:int):
    with get_session() as s:
        row=s.get(LeadCandidate,lid)
        if not row:raise HTTPException(404,'候选不存在')
        return lead_dict(row,True)


@router.post('/api/leads/{lid:int}/review')
async def review_lead(lid:int,body:ReviewIn,request:Request):
    actor=admin(request);key_required(request)
    with get_session() as s:
        def create():
            row=s.get(LeadCandidate,lid)
            if not row:raise HTTPException(404,'候选不存在')
            if row.state!='candidate' or row.version!=body.version or row.digest!=body.digest or digest(row)!=body.digest:
                raise HTTPException(409,'候选版本或状态已变化，请重新核对')
            if body.decision=='approve' and not body.owner_label.strip():raise HTTPException(422,'请填写负责人标签（非飞书用户 ID）')
            if not body.note.strip():raise HTTPException(422,'请填写复核说明')
            row.owner_label=body.owner_label.strip();row.review_note=body.note.strip();row.reviewed_at=datetime.utcnow()
            row.state='approved' if body.decision=='approve' else 'rejected';row.approved_digest=row.digest if body.decision=='approve' else ''
            s.add(row);s.add(RiskAdminAudit(action='lead_reviewed',account_id=row.account_id,actor=actor,
                detail=json.dumps({'id':lid,'decision':body.decision,'version':body.version,'digest':body.digest})))
            s.flush();return lead_dict(row)
        result,_=submit_once(s,request=request,scope=f'lead:review:{lid}',body=body,create=create)
        return result


def customer_payload(row):
    if row.state not in {'approved','recorded_mock'} or row.approved_digest!=row.digest or digest(row)!=row.digest:
        raise HTTPException(409,'尚未人工确认或内容已变化，不能入表')
    return {k:getattr(row,k) for k in ('external_key','platform','account_id','work_id','comment_id','source_nickname','comment_text','owner_label')} | {'reviewed_at':utc_iso(row.reviewed_at)}


@router.get('/api/leads/{lid:int}/crm-preview')
async def preview_crm(lid:int):
    with get_session() as s:
        row=s.get(LeadCandidate,lid)
        if not row:raise HTTPException(404,'候选不存在')
        return {'mode':'local_mock','real_table_configured':False,'fields':map_fields(customer_payload(row)),
                'mapping':DEFAULT_MAPPING,'external_write_triggered':False,'owner_field_type':'text_label_not_feishu_person'}


@router.post('/api/leads/{lid:int}/crm-mock')
async def mock_crm(lid:int,body:VersionIn,request:Request):
    key_required(request)
    with get_session() as s:
        def create():
            row=s.get(LeadCandidate,lid)
            if not row:raise HTTPException(404,'候选不存在')
            if row.version!=body.version or row.digest!=body.digest:raise HTTPException(409,'候选版本已变化')
            fields=map_fields(customer_payload(row))
            existing=s.exec(select(MockCustomerRecord).where(MockCustomerRecord.external_key==row.external_key)).first()
            created=existing is None
            record=existing or MockCustomerRecord(external_key=row.external_key,lead_id=lid,
                        operation_token=str(uuid.uuid4()),fields_json=json.dumps(fields,ensure_ascii=False))
            s.add(record);row.state='recorded_mock';s.add(row);s.flush()
            return {'id':row.id,'mock_record_id':record.id,'mode':'local_mock','created':created,
                    'deduplicated':not created,'external_write_triggered':False,'real_table_configured':False,'status':'recorded_mock'}
        result,_=submit_once(s,request=request,scope=f'lead:mock:{lid}',body=body,create=create)
        return result


@router.get('/api/client-receipts/{operation}/{key}')
async def lookup_receipt(operation:str,key:str):
    from .models import TaskSubmission,PublishTask,SyncRequest
    import re
    scopes={'publish':'publish:draft','sync':'sync','asset':'asset','leads':'lead-scan'}
    if operation not in scopes or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{15,127}',key):
        raise HTTPException(422,'回执类型或编号不正确')
    identity=hashlib.sha256(f'{scopes[operation]}\0{key}'.encode()).hexdigest()
    with get_session() as s:
        row=s.get(TaskSubmission,identity)
        if not row:raise HTTPException(404,'暂未查到回执，不能据此认定提交未发生')
        result=json.loads(row.response_json)
        data={k:result[k] for k in ('id','status','account_id','work_id','kind','created','updated','scanned','matched','is_demo','demo_only') if k in result}
        if operation in {'publish','sync'} and result.get('id'):
            task=s.get(PublishTask if operation=='publish' else SyncRequest,result['id'])
            data['status']=task.status if task and task.created_at==row.resource_created_at else 'resource_missing'
        return {'operation':operation,'request_key':key,'receipt_found':True,'source':'existing_hub_database',**data}


class DemoLeadIn(StrictBody):
    account_id: StrictInt = Field(gt=0)


@router.post('/api/leads/demo')
async def demo_lead(body:DemoLeadIn,request:Request):
    key_required(request)
    with get_session() as s:
        def create():
            account=s.get(DouyinAccount,body.account_id)
            if not account or account.platform!='douyin':raise HTTPException(422,'请选择抖音账号')
            sk='demo:'+str(body.account_id)+':v1'
            row=s.exec(select(LeadCandidate).where(LeadCandidate.source_key==sk)).first()
            if row is None:
                row=LeadCandidate(source_key=sk,external_key='demo_hlm_'+hashlib.sha256(sk.encode()).hexdigest(),
                    platform='douyin',account_id=body.account_id,work_id=0,item_id='demo-only',comment_id='demo-only',
                    source_nickname='演示用户（不是真实客户）',comment_text='【本机演练数据】我想了解课程费用，怎么报名？',
                    signal_tags='["price","consult"]',is_demo=True)
                row.digest=digest(row);s.add(row);s.flush()
            return lead_dict(row)
        result,_=submit_once(s,request=request,scope='lead:demo',body=body,create=create)
        return result
