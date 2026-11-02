"""Content -> per-account drafts; employee targets -> human-reviewed evidence.

All durable state is in the existing Hub. Local owner remains trusted, scoped
team credentials never gain access to legacy APIs. No public team gateway,
real employee identities, cloud permissions or publish approval is enabled here.
"""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
import hashlib,io,json,secrets,uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter,HTTPException,Request,UploadFile,File,Form
from fastapi.responses import Response,JSONResponse
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictInt
from PIL import Image
from sqlmodel import select

from .db import get_session
from .models import DouyinAccount,LocalMediaAsset,PublishTask,LeadCandidate
from .business_models import *
from .submissions import submit_once
from .scheduling import utc_iso

router=APIRouter(prefix='/api/operations')
ROLES={'admin','operator','employee','reviewer','sales','maintainer'}
MAX_PROOF=8*1024*1024

def uid(prefix):return prefix+'_'+uuid.uuid4().hex
def encode(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(v):return hashlib.sha256(encode(v).encode()).hexdigest()
def now():return datetime.utcnow()
def when(value):
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        if not d.tzinfo:raise ValueError()
        return d.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError,AttributeError):raise HTTPException(422,'时间必须包含时区') from None

class Strict(BaseModel):model_config=ConfigDict(extra='forbid')

def audit(s,actor,action,object_id,detail=None):
    s.add(BusinessAudit(id=uid('audit'),actor_id=actor,action=action,object_id=str(object_id),detail_json=encode(detail or {})))

def principal(request,roles=()):
    auth=request.headers.get('authorization','')
    if auth:
        if not auth.startswith('Bearer cht_') or len(auth)>160:raise HTTPException(401,'团队凭据无效')
        hashed=hashlib.sha256(auth[7:].encode()).hexdigest()
        with get_session() as s:
            cred=s.exec(select(TeamCredential).where(TeamCredential.token_hash==hashed)).first()
            actor=s.get(TeamActor,cred.actor_id) if cred else None
            if not cred or not cred.enabled or cred.expires_at<=now() or not actor or not actor.enabled:raise HTTPException(401,'团队授权失效')
            grants=set(json.loads(actor.roles_json));aid=actor.id;label=actor.label
        if roles and 'admin' not in grants and not grants.intersection(roles):raise HTTPException(403,'当前角色没有此操作权限')
        return {'id':aid,'label':label,'roles':grants,'local_owner':False}
    if request.headers.get('x-creatorhub-team-gateway'):raise HTTPException(401,'团队入口必须绑定可信身份')
    from .main import _require_risk_admin
    _require_risk_admin(request)  # Never trust client-supplied actor headers.
    return {'id':'local_owner','label':'当前本机管理员','roles':{'admin'},'local_owner':True}

class TeamBoundaryMiddleware:
    """A scoped credential is never accepted on old, single-owner Hub routes."""
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']=='http':
            req=Request(scope)
            if req.headers.get('authorization','').startswith('Bearer cht_') and not scope.get('path','').startswith('/api/operations/'):
                await JSONResponse({'detail':'团队凭据只允许业务工作流接口'},status_code=403)(scope,receive,send);return
        await self.app(scope,receive,send)

def once(request,scope,body,fn):
    if not request.headers.get('Idempotency-Key'):raise HTTPException(422,'需要稳定的 Idempotency-Key')
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        data,_=submit_once(s,request=request,scope='business:'+principal(request)['id']+':'+scope,body=body,create=lambda:fn(s))
        return data

def exists(s,model,key):
    row=s.get(model,key)
    if not row:raise HTTPException(404,'业务记录不存在')
    return row

def check_actor(s,key,role=None):
    if key=='local_owner':return
    a=exists(s,TeamActor,key)
    if not a.enabled or (role and role not in json.loads(a.roles_json)):raise HTTPException(422,'参与人未启用或角色不匹配')

def row_dict(row,exclude=()):
    data={k:getattr(row,k) for k in type(row).model_fields}
    for key in list(data):
        if key in exclude:data.pop(key)
        elif isinstance(data[key],datetime):data[key]=utc_iso(data[key])
    return data

@router.get('/session')
def session_info(request:Request):
    p=principal(request)
    return {**p,'roles':sorted(p['roles']),'remote_team_gateway_enabled':False,
            'boundary':'trusted local owner; scoped team credentials apply to operations only, not isolation from another program owning the same OS shell'}

@router.get('/actors')
def actors(request:Request):
    p=principal(request,{'admin','operator','reviewer','maintainer'})
    with get_session() as s:return [row_dict(a) for a in s.exec(select(TeamActor).order_by(TeamActor.created_at)).all()]

class ActorIn(Strict):
    label:str=Field(min_length=1,max_length=80)
    roles:list[str]=Field(min_length=1,max_length=6)
    is_demo:StrictBool=False

@router.post('/actors')
def actor_create(body:ActorIn,request:Request):
    p=principal(request,{'admin'})
    if set(body.roles)-ROLES:raise HTTPException(422,'未知角色')
    def create(s):
        row=TeamActor(id=uid('actor'),label=body.label,roles_json=encode(sorted(set(body.roles))),is_demo=body.is_demo)
        s.add(row);audit(s,p['id'],'actor_created',row.id,{'roles':body.roles,'is_demo':body.is_demo});s.flush();return row_dict(row)
    return once(request,'actor',body,create)

class CredentialIn(Strict):
    hours:StrictInt=Field(default=24,gt=0,le=168)
    acknowledge_scope:StrictBool=False

@router.post('/actors/{aid}/credential')
def credential_issue(aid:str,body:CredentialIn,request:Request):
    p=principal(request,{'admin'})
    # Human-only bootstrap; deliberately not a CLI/model command. No token in
    # TaskSubmission receipts or audits, and no automatic issuance at startup.
    if not body.acknowledge_scope:raise HTTPException(422,'需明确确认此角色及期限授权')
    from .submissions import _identity
    identity=_identity(request,'credential:'+p['id']+':'+aid,body)
    if not identity:raise HTTPException(422,'需要稳定的 Idempotency-Key')
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        cid='cred_'+identity[0][:32]
        if s.get(TeamCredential,cid):raise HTTPException(409,'该授权请求已执行；凭据仅展示一次，丢失后先吊销再重新授权')
        a=exists(s,TeamActor,aid)
        if not a.enabled:raise HTTPException(409,'参与人已停用')
        token='cht_'+secrets.token_urlsafe(32)
        c=TeamCredential(id=cid,actor_id=aid,token_hash=hashlib.sha256(token.encode()).hexdigest(),expires_at=now()+timedelta(hours=body.hours))
        s.add(c);audit(s,p['id'],'credential_issued',c.id,{'actor_id':aid,'hours':body.hours});s.commit()
        return {'credential_id':c.id,'token':token,'expires_at':utc_iso(c.expires_at),'show_once':True}

class ActorStateIn(Strict):enabled:StrictBool
@router.post('/actors/{aid}/state')
def actor_state(aid:str,body:ActorStateIn,request:Request):
    p=principal(request,{'admin'})
    def update(s):
        a=exists(s,TeamActor,aid);a.enabled=body.enabled;s.add(a)
        if not a.enabled:
            for c in s.exec(select(TeamCredential).where(TeamCredential.actor_id==aid)).all():c.enabled=False;s.add(c)
        audit(s,p['id'],'actor_state',aid,{'enabled':body.enabled});return row_dict(a)
    return once(request,'actor-state:'+aid,body,update)

class ContentIn(Strict):
    label:str=Field(min_length=1,max_length=120)
    owner_actor_id:str='local_owner'

@router.post('/contents')
def content_create(body:ContentIn,request:Request):
    p=principal(request,{'operator'})
    def create(s):
        check_actor(s,body.owner_actor_id)
        c=BusinessContent(id=uid('content'),label=body.label,owner_actor_id=body.owner_actor_id);s.add(c);audit(s,p['id'],'content_created',c.id);return row_dict(c)
    return once(request,'content',body,create)

class VersionIn(Strict):
    title:str=Field(min_length=1,max_length=120)
    description:str=Field(default='',max_length=10000)
    tags:str=Field(default='',max_length=1000)
    asset_ids:list[str]=Field(min_length=1,max_length=9)

def assets_signature(s,ids):
    from .publish_review import file_hash
    values=[];demo=False;media_type=None;count=0
    if len(ids)!=len(set(ids)):raise HTTPException(422,'素材编号重复')
    for aid in ids:
        a=exists(s,LocalMediaAsset,aid);demo=demo or a.is_demo
        if media_type and a.media_type!=media_type:raise HTTPException(422,'一个版本不能混合图文和视频')
        media_type=a.media_type
        manifest=json.loads(a.manifest_json);count+=len(manifest)
        if count>9 or not manifest or (media_type=='video' and count>1):raise HTTPException(422,'素材总数不符合图文/视频草稿约定')
        for m in manifest:
            path=Path(m['path'])
            if path.is_symlink() or not path.is_file() or path.stat().st_size!=m['size'] or file_hash(path)!=m['sha256']:raise HTTPException(409,'素材副本变化或不可用')
        values.append({'id':aid,'media_type':a.media_type,'manifest':[{k:m[k] for k in ('size','sha256')} for m in manifest]})
    return digest(values),demo,media_type

def intact_version(s,vid):
    v=exists(s,ContentVersion,vid);sig,_,kind=assets_signature(s,json.loads(v.asset_ids_json))
    if sig!=v.assets_digest or digest({'content_id':v.content_id,'title':v.title,'description':v.description,'tags':v.tags,'asset_ids':json.loads(v.asset_ids_json),'assets_digest':sig})!=v.digest:raise HTTPException(409,'内容版本或素材摘要已变化')
    return v,kind

@router.post('/contents/{cid}/versions')
def version_create(cid:str,body:VersionIn,request:Request):
    p=principal(request,{'operator'})
    def create(s):
        exists(s,BusinessContent,cid);sig,demo,_=assets_signature(s,body.asset_ids)
        revisions=s.exec(select(ContentVersion.revision).where(ContentVersion.content_id==cid)).all()
        d={'content_id':cid,**body.model_dump(),'assets_digest':sig}
        v=ContentVersion(id=uid('version'),content_id=cid,revision=max(revisions or [0])+1,title=body.title,description=body.description,tags=body.tags,
            asset_ids_json=encode(body.asset_ids),assets_digest=sig,digest=digest(d),is_demo=demo)
        s.add(v);audit(s,p['id'],'content_version_created',v.id,{'revision':v.revision});return row_dict(v)
    return once(request,'content-version:'+cid,body,create)

@router.get('/contents')
def contents(request:Request):
    principal(request,{'operator','reviewer','maintainer'})
    with get_session() as s:return {'items':[row_dict(r) for r in s.exec(select(BusinessContent).order_by(BusinessContent.created_at.desc()).limit(200)).all()],
        'versions':[row_dict(r) for r in s.exec(select(ContentVersion).order_by(ContentVersion.created_at.desc()).limit(500)).all()],
        'variants':[row_dict(r) for r in s.exec(select(ContentVariant).order_by(ContentVariant.created_at.desc()).limit(500)).all()]}

class PersonaIn(Strict):
    alias:str=Field(default='',max_length=80)
    persona:str=Field(default='',max_length=2000)
    owner_actor_id:str='local_owner'
@router.post('/accounts/{aid}/persona')
def persona_update(aid:int,body:PersonaIn,request:Request):
    p=principal(request,{'operator'})
    def update(s):
        exists(s,DouyinAccount,aid);check_actor(s,body.owner_actor_id)
        row=s.get(AccountPersona,aid) or AccountPersona(account_id=aid)
        for k,v in body.model_dump().items():setattr(row,k,v)
        row.updated_at=now();s.add(row);audit(s,p['id'],'persona_updated',aid);return row_dict(row)
    return once(request,'persona:'+str(aid),body,update)

@router.get('/personas')
def personas(request:Request):
    principal(request,{'operator','reviewer'})
    with get_session() as s:return [row_dict(r) for r in s.exec(select(AccountPersona)).all()]

class VariantIn(Strict):
    account_id:StrictInt=Field(gt=0)
    title:str=Field(min_length=1,max_length=20)
    description:str=Field(default='',max_length=10000)
    topics:str=Field(default='',max_length=1000)
    visibility:str=Field(default='private',pattern='^(public|friends|private)$')
@router.post('/versions/{vid}/variants')
def variant_create(vid:str,body:VariantIn,request:Request):
    p=principal(request,{'operator'})
    def create(s):
        intact_version(s,vid);a=exists(s,DouyinAccount,body.account_id)
        if a.platform!='douyin':raise HTTPException(422,'当前发布草稿执行仍仅支持抖音，其余平台保留人工流程')
        row=ContentVariant(id=uid('variant'),version_id=vid,platform=a.platform,digest=digest({'version_id':vid,**body.model_dump()}),**body.model_dump())
        s.add(row);audit(s,p['id'],'variant_created',row.id);return row_dict(row)
    return once(request,'variant:'+vid,body,create)

class PlanIn(Strict):
    label:str=Field(min_length=1,max_length=120)
    variant_ids:list[str]=Field(min_length=1,max_length=20)
def intact_variant(s,vid):
    v=exists(s,ContentVariant,vid)
    expected=digest({'version_id':v.version_id,**{k:getattr(v,k) for k in ('account_id','title','description','topics','visibility')}})
    if expected!=v.digest:raise HTTPException(409,'账号变体摘要已变化')
    return v

@router.post('/plans')
def plan_create(body:PlanIn,request:Request):
    p=principal(request,{'operator'})
    def create(s):
        if len(set(body.variant_ids))!=len(body.variant_ids):raise HTTPException(422,'同一变体不能重复加入一个批次')
        accounts=[]
        for vid in body.variant_ids:
            v=intact_variant(s,vid);intact_version(s,v.version_id);accounts.append(v.account_id)
        if len(set(accounts))!=len(accounts):raise HTTPException(422,'同一批次每个账号只保留一个明确变体')
        plan=PublishPlan(id=uid('plan'),label=body.label,actor_id=p['id']);s.add(plan)
        for vid in body.variant_ids:s.add(PublishPlanItem(id=uid('item'),plan_id=plan.id,variant_id=vid))
        audit(s,p['id'],'publish_plan_created',plan.id);return row_dict(plan)
    return once(request,'plan',body,create)

@router.get('/plans')
def plans(request:Request):
    principal(request,{'operator','reviewer'})
    with get_session() as s:return {'items':[row_dict(r) for r in s.exec(select(PublishPlan).order_by(PublishPlan.created_at.desc()).limit(200)).all()],
        'targets':[row_dict(r) for r in s.exec(select(PublishPlanItem).limit(1000)).all()]}

class EmptyIn(Strict):pass

@router.post('/plans/{pid}/drafts')
async def plan_drafts(pid:str,body:EmptyIn,request:Request):
    from .submissions import _identity
    if not _identity(request,'plan-drafts:'+pid,body):raise HTTPException(422,'需要稳定的 Idempotency-Key')
    p=principal(request,{'operator'})
    from . import main
    with get_session() as s:
        plan=exists(s,PublishPlan,pid);items=s.exec(select(PublishPlanItem).where(PublishPlanItem.plan_id==pid)).all()
    results=[]
    for item in items:
        if item.publish_task_id:
            with get_session() as s:
                if not s.get(PublishTask,item.publish_task_id):raise HTTPException(410,'原草稿已删除，批次不自动重建')
            results.append({'item_id':item.id,'publish_task_id':item.publish_task_id});continue
        with get_session() as s:
            v=intact_variant(s,item.variant_id);version,kind=intact_version(s,v.version_id)
            args={'account_id':v.account_id,'media_type':kind,'asset_ids':json.loads(version.asset_ids_json),'title':v.title,
                  'desc':v.description,'topics':v.topics,'visibility':v.visibility,'allow_save':False,'demo_only':version.is_demo}
        scope=dict(request.scope);scope['headers']=[(k,val) for k,val in request.scope['headers'] if k.lower()!=b'idempotency-key']
        scope['headers'].append((b'idempotency-key',('plan-draft-'+digest({'plan':pid,'item':item.id})).encode()))
        result=await main.create_publish_draft(main.PublishDraftIn(**args),Request(scope))
        with get_session() as s:
            live=exists(s,PublishPlanItem,item.id);live.publish_task_id=result['id'];live.status='draft';s.add(live)
            audit(s,p['id'],'plan_draft_materialized',item.id,{'publish_task_id':result['id']});s.commit()
        results.append({'item_id':item.id,'publish_task_id':result['id']})
    with get_session() as s:
        plan=exists(s,PublishPlan,pid);plan.status='drafts_created';s.add(plan);s.commit()
    return {'id':pid,'status':'drafts_created','items':results,'actual_published':False,'human_approval_required':True}

class DistributionIn(Strict):
    version_id:str
    employee_id:str
    due_at:str
    targets:list[dict[str,str]]=Field(min_length=1,max_length=100)
    note:str=Field(default='',max_length=1000)

def task_access(s,tid,p,*,proof=False):
    t=exists(s,DistributionTask,tid)
    privileged={'admin','reviewer'} if proof else {'admin','operator','reviewer','maintainer'}
    if not p['roles'].intersection(privileged) and t.employee_id!=p['id']:raise HTTPException(403,'只能访问自己的任务')
    return t

def task_dict(s,t,p):
    targets=s.exec(select(DistributionTarget).where(DistributionTarget.task_id==t.id)).all()
    d=row_dict(t);d['targets']=[row_dict(x) for x in targets]
    verified=sum(x.status=='verified' for x in targets);canceled=sum(x.status=='canceled' for x in targets)
    d.update(total_targets=len(targets),verified_targets=verified,canceled_targets=canceled,
             completion='completed' if verified+canceled==len(targets) and verified else 'canceled' if canceled==len(targets) else 'partial' if verified else 'incomplete',
             overdue=t.due_at<now() and verified+canceled<len(targets),customer_read_verified=False,sale_verified=False)
    return d

@router.post('/distribution')
def distribution_create(body:DistributionIn,request:Request):
    p=principal(request,{'operator'})
    due=when(body.due_at)
    if due<=now():raise HTTPException(422,'任务期限应在未来')
    for target in body.targets:
        if set(target)!={'channel','label'} or target['channel'] not in {'wechat','feishu','manual'} or not 1<=len(target['label'].strip())<=160:raise HTTPException(422,'目标需明确渠道和名称；不自动读取通讯录')
    if len({encode(x) for x in body.targets})!=len(body.targets):raise HTTPException(422,'目标重复')
    def create(s):
        v,_=intact_version(s,body.version_id);check_actor(s,body.employee_id,'employee')
        t=DistributionTask(id=uid('distribution'),version_id=v.id,version_digest=v.digest,employee_id=body.employee_id,assigned_by=p['id'],due_at=due,note=body.note)
        s.add(t)
        for target in body.targets:s.add(DistributionTarget(id=uid('target'),task_id=t.id,**target))
        audit(s,p['id'],'distribution_assigned',t.id,{'employee_id':body.employee_id,'targets':len(body.targets)})
        # Durable notification interface is separate from assignment success.
        from .delivery_workflows import add_event
        add_event(s,'task_notice',t.id,body.employee_id,{'task_id':t.id,'summary':'有一条内容分发任务待领取'},dedupe='task:'+t.id)
        s.flush();return task_dict(s,t,p)
    return once(request,'distribution',body,create)

@router.get('/distribution')
def distribution_list(request:Request):
    p=principal(request,{'operator','employee','reviewer','maintainer'})
    with get_session() as s:
        q=select(DistributionTask)
        if not p['roles'].intersection({'admin','operator','reviewer','maintainer'}):q=q.where(DistributionTask.employee_id==p['id'])
        return {'items':[task_dict(s,t,p) for t in s.exec(q.order_by(DistributionTask.created_at.desc()).limit(200)).all()]}

@router.get('/distribution/{tid}')
def distribution_show(tid:str,request:Request):
    p=principal(request)
    with get_session() as s:return task_dict(s,task_access(s,tid,p),p)

@router.get('/distribution/{tid}/materials')
def distribution_materials(tid:str,request:Request):
    p=principal(request)
    with get_session() as s:
        t=task_access(s,tid,p);v,_=intact_version(s,t.version_id)
        return {'task_id':tid,'version_id':v.id,'version_digest':v.digest,'title':v.title,'description':v.description,'tags':v.tags,
                'assets':[{'id':aid,'media_count':len(json.loads(exists(s,LocalMediaAsset,aid).manifest_json))} for aid in json.loads(v.asset_ids_json)]}

@router.get('/distribution/{tid}/assets/{aid}/{index}')
def distribution_asset(tid:str,aid:str,index:int,request:Request):
    p=principal(request)
    from .video_backup import open_source
    from . import main
    with get_session() as s:
        t=task_access(s,tid,p);v,_=intact_version(s,t.version_id)
        if aid not in json.loads(v.asset_ids_json):raise HTTPException(403,'素材不属于该任务')
        a=exists(s,LocalMediaAsset,aid);manifest=json.loads(a.manifest_json)
        if index<0 or index>=len(manifest):raise HTTPException(404,'素材不存在')
        m=manifest[index];root=Path(main.UPLOAD_DIR).resolve();path=Path(m['path']).resolve()
        if not path.is_relative_to(root):raise HTTPException(403,'素材不在 Hub 管理目录')
        # FileResponse avoids copying large videos into memory; path/digest were
        # checked by intact_version. No arbitrary filesystem browsing exists.
        from fastapi.responses import FileResponse
        return FileResponse(path,headers={'Content-Disposition':'attachment; filename="material'+path.suffix+'"'})

class TargetActionIn(Strict):
    revision:StrictInt=Field(gt=0)
    note:str=Field(default='',max_length=1000)

@router.post('/targets/{target_id}/claim')
def target_claim(target_id:str,body:TargetActionIn,request:Request):
    p=principal(request,{'employee'})
    def update(s):
        target=exists(s,DistributionTarget,target_id);t=task_access(s,target.task_id,p)
        if t.employee_id!=p['id'] and 'admin' not in p['roles']:raise HTTPException(403,'只能领取自己的目标')
        if target.revision!=body.revision or target.status not in {'assigned','returned'}:raise HTTPException(409,'目标状态或版本已变化')
        target.status='claimed';target.revision+=1;s.add(target);audit(s,p['id'],'target_claimed',target_id);return row_dict(target)
    return once(request,'target-claim:'+target_id,body,update)

class TaskChangeIn(Strict):
    revision:StrictInt=Field(gt=0)
    action:str=Field(pattern='^(extend|reassign|cancel)$')
    due_at:str=''
    employee_id:str=''
    reason:str=Field(min_length=1,max_length=1000)
@router.post('/distribution/{tid}/change')
def distribution_change(tid:str,body:TaskChangeIn,request:Request):
    p=principal(request,{'operator'})
    def update(s):
        t=task_access(s,tid,p)
        if t.revision!=body.revision:raise HTTPException(409,'任务版本已变化')
        targets=s.exec(select(DistributionTarget).where(DistributionTarget.task_id==tid)).all()
        if body.action=='extend':
            due=when(body.due_at)
            if due<=now():raise HTTPException(422,'延期时间必须在未来')
            t.due_at=due
        elif body.action=='reassign':
            check_actor(s,body.employee_id,'employee')
            if any(x.status in {'submitted','verified'} for x in targets):raise HTTPException(409,'已有凭证或核验的任务不直接转派，应先由核验人处理')
            t.employee_id=body.employee_id
            for x in targets:x.status='assigned';x.revision+=1;s.add(x)
        else:
            for x in targets:
                if x.status!='verified':x.status='canceled';x.revision+=1;x.last_note=body.reason;s.add(x)
            t.status='canceled'
        t.revision+=1;t.note=body.reason;s.add(t)
        from .delivery_workflows import add_event
        add_event(s,'task_notice',tid,t.employee_id,{'task_id':tid,'summary':'分发任务已变更，请核对最新目标和期限'},dedupe='task-change:'+tid+':'+str(t.revision))
        audit(s,p['id'],'distribution_'+body.action,tid,{'reason':body.reason});return task_dict(s,t,p)
    return once(request,'task-change:'+tid,body,update)

@router.post('/targets/{target_id}/proofs')
async def proof_submit(target_id:str,request:Request,file:UploadFile=File(...),note:str=Form(...),revision:int=Form(...)):
    p=principal(request,{'employee'})
    if set((await request.form()).keys())-{'file','note','revision'}:raise HTTPException(422,'不接受身份或其他额外字段')
    if not 1<=len(note.strip())<=1000:raise HTTPException(422,'请填写凭证说明')
    raw=await file.read(MAX_PROOF+1)
    if not raw or len(raw)>MAX_PROOF:raise HTTPException(422,'截图不能超过8MiB')
    try:
        im=Image.open(io.BytesIO(raw));fmt=im.format
        if fmt not in {'PNG','JPEG','WEBP'} or im.width*im.height>24_000_000:raise ValueError()
        im.verify()
    except Exception:raise HTTPException(422,'只接受格式完整、尺寸受限的PNG/JPEG/WebP截图') from None
    checksum=hashlib.sha256(raw).hexdigest();body={'sha256':checksum,'size':len(raw),'revision':revision,'note':note}
    def create(s):
        target=exists(s,DistributionTarget,target_id);t=task_access(s,target.task_id,p)
        if t.employee_id!=p['id'] and 'admin' not in p['roles']:raise HTTPException(403,'不能替其他员工提交凭证')
        if target.revision!=revision or target.status not in {'assigned','claimed','returned'}:raise HTTPException(409,'目标已变化或已提交，请刷新')
        v,_=intact_version(s,t.version_id)
        if v.digest!=t.version_digest:raise HTTPException(409,'材料版本不一致')
        reused=s.exec(select(DistributionProof.id).where(DistributionProof.sha256==checksum)).first() is not None
        from . import main
        root=Path(main.cfg.db_path).absolute().parent/'business-proofs';root.mkdir(mode=0o700,exist_ok=True)
        pid=uid('proof');path=root/(pid+{'PNG':'.png','JPEG':'.jpg','WEBP':'.webp'}[fmt])
        with path.open('xb') as out:
            import os
            os.chmod(path,0o600);out.write(raw)
        proof=DistributionProof(id=pid,target_id=target_id,task_id=t.id,version_digest=t.version_digest,submitted_by=p['id'],file_path=str(path),
            mime_type={'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp'}[fmt],sha256=checksum,size=len(raw),note=note,duplicate_suspected=reused)
        s.add(proof);target.status='submitted';target.latest_proof_id=pid;target.revision+=1;s.add(target)
        audit(s,p['id'],'proof_submitted',pid,{'target_id':target_id,'duplicate_suspected':reused});return row_dict(proof,{'file_path'})
    return once(request,'proof:'+target_id,body,create)

@router.get('/proofs/{pid}')
def proof_show(pid:str,request:Request):
    p=principal(request)
    with get_session() as s:
        proof=exists(s,DistributionProof,pid);task_access(s,proof.task_id,p,proof=True)
        return {**row_dict(proof,{'file_path'}),'reviews':[row_dict(x) for x in s.exec(select(ProofVerification).where(ProofVerification.proof_id==pid)).all()]}

@router.get('/proofs/{pid}/file')
def proof_file(pid:str,request:Request):
    p=principal(request)
    from .video_backup import open_source
    with get_session() as s:
        proof=exists(s,DistributionProof,pid);task_access(s,proof.task_id,p,proof=True)
        from . import main
        root=Path(main.cfg.db_path).absolute().parent/'business-proofs';path=Path(proof.file_path)
        if path.parent!=root:raise HTTPException(409,'凭证位置变化')
        with open_source(root,path.name) as f:raw=f.read(MAX_PROOF+1)
        if len(raw)!=proof.size or hashlib.sha256(raw).hexdigest()!=proof.sha256:raise HTTPException(409,'凭证摘要变化')
        return Response(raw,media_type=proof.mime_type,headers={'Content-Disposition':'inline; filename="proof'+path.suffix+'"'})

class VerificationIn(Strict):
    target_revision:StrictInt=Field(gt=0)
    proof_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    decision:str=Field(pattern='^(accept|return|reject|revoke)$')
    reason:str=Field(min_length=1,max_length=1000)
    acknowledge_duplicate:StrictBool=False

@router.post('/proofs/{pid}/review')
def proof_review(pid:str,body:VerificationIn,request:Request):
    p=principal(request,{'reviewer'})
    if not body.reason.strip():raise HTTPException(422,'请填写核验依据')
    def update(s):
        proof=exists(s,DistributionProof,pid);t=task_access(s,proof.task_id,p,proof=True);target=exists(s,DistributionTarget,proof.target_id)
        if proof.submitted_by==p['id']:raise HTTPException(403,'提交人不能核验自己的凭证')
        if target.revision!=body.target_revision or proof.sha256!=body.proof_sha256 or target.latest_proof_id!=pid:raise HTTPException(409,'凭证或目标版本变化')
        if body.decision=='revoke':
            if target.status!='verified':raise HTTPException(409,'只能撤销已核验目标')
            target.status='returned';proof.state='revoked';target.verified_at=None
        else:
            if target.status!='submitted' or proof.state!='submitted':raise HTTPException(409,'当前凭证不在待核验状态')
            if body.decision=='accept':
                if proof.duplicate_suspected and not body.acknowledge_duplicate:raise HTTPException(409,'截图疑似复用，核验人需明确说明并确认')
                from .video_backup import open_source
                path=Path(proof.file_path)
                with open_source(path.parent,path.name) as f:raw=f.read(MAX_PROOF+1)
                if hashlib.sha256(raw).hexdigest()!=proof.sha256 or len(raw)!=proof.size:raise HTTPException(409,'凭证文件已变化')
                intact_version(s,t.version_id);target.status='verified';proof.state='accepted';target.verified_at=now()
            else:target.status='returned';proof.state=body.decision
        target.last_note=body.reason;target.revision+=1;s.add(target);s.add(proof)
        review=ProofVerification(id=uid('review'),proof_id=pid,target_id=target.id,reviewer_id=p['id'],decision=body.decision,reason=body.reason)
        s.add(review)
        from .delivery_workflows import add_event
        add_event(s,'task_notice',t.id,t.employee_id,{'task_id':t.id,'summary':'分发凭证核验状态已更新，请查看原目标'},dedupe='proof-review:'+review.id)
        audit(s,p['id'],'proof_'+body.decision,pid,{'review_id':review.id,'duplicate_ack':body.acknowledge_duplicate})
        return {'target':row_dict(target),'review':row_dict(review),'customer_read_verified':False,'sale_verified':False}
    return once(request,'proof-review:'+pid,body,update)

class ManualIn(Strict):
    variant_id:str
    external_url:str=Field(max_length=2000)
    published_at:str
    note:str=Field(min_length=1,max_length=1000)
@router.post('/manual-publishes')
def manual_publish(body:ManualIn,request:Request):
    p=principal(request,{'operator'})
    parsed=urlsplit(body.external_url)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.hostname not in {'www.douyin.com','v.douyin.com','creator.douyin.com'} or parsed.query or parsed.fragment:raise HTTPException(422,'人工回填需无凭据/查询参数的抖音HTTPS作品链接')
    stamp=when(body.published_at)
    if stamp>now()+timedelta(minutes=5):raise HTTPException(422,'发布时间不能在未来')
    def create(s):
        v=intact_variant(s,body.variant_id);intact_version(s,v.version_id)
        old=s.exec(select(ManualPublishRecord).where(ManualPublishRecord.account_id==v.account_id,ManualPublishRecord.external_url==body.external_url)).first()
        if old:return row_dict(old)
        row=ManualPublishRecord(id=uid('manual'),variant_id=v.id,account_id=v.account_id,external_url=body.external_url,published_at=stamp,recorded_by=p['id'],note=body.note)
        s.add(row);audit(s,p['id'],'manual_publish_recorded',row.id);return row_dict(row)
    return once(request,'manual-publish',body,create)

class ManualReviewIn(Strict):
    decision:str=Field(pattern='^(verify|reject)$')
    note:str=Field(min_length=1,max_length=1000)
@router.post('/manual-publishes/{mid}/review')
def manual_review(mid:str,body:ManualReviewIn,request:Request):
    p=principal(request,{'reviewer'})
    def update(s):
        row=exists(s,ManualPublishRecord,mid)
        if row.status!='pending_review':raise HTTPException(409,'记录已经复核')
        if row.recorded_by==p['id']:raise HTTPException(403,'记录人不能自核验')
        row.status='verified' if body.decision=='verify' else 'rejected';row.reviewed_by=p['id'];row.review_note=body.note;s.add(row)
        audit(s,p['id'],'manual_publish_'+body.decision,mid);return row_dict(row)
    return once(request,'manual-review:'+mid,body,update)

@router.get('/manual-publishes')
def manual_list(request:Request):
    principal(request,{'operator','reviewer'})
    with get_session() as s:return [row_dict(r) for r in s.exec(select(ManualPublishRecord).order_by(ManualPublishRecord.created_at.desc()).limit(200)).all()]

@router.get('/audit')
def audit_list(request:Request):
    principal(request,{'admin','maintainer'})
    with get_session() as s:return [row_dict(r) for r in s.exec(select(BusinessAudit).order_by(BusinessAudit.created_at.desc()).limit(200)).all()]

@router.get('/assets')
def business_assets(request:Request):
    principal(request,{'operator','reviewer'})
    from .operations_workflows import asset_dict
    with get_session() as s:return [asset_dict(r) for r in s.exec(select(LocalMediaAsset).order_by(LocalMediaAsset.created_at.desc()).limit(200)).all()]

@router.get('/accounts')
def business_accounts(request:Request):
    principal(request,{'operator','reviewer'})
    with get_session() as s:return [{k:getattr(r,k) for k in ('id','nickname','platform','status')} for r in s.exec(select(DouyinAccount)).all()]
