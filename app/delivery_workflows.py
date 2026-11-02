"""Durable record/notice outbox. No credentials, grants, table or sends by default.

A claimed delivery interrupted by process death is UNKNOWN, never blindly
replayed. Record and owner notice have separate identities and retry lifecycles.
Typed owner/date fields require explicit mapping and a verified app-scoped owner.
"""
from __future__ import annotations
import hashlib,json,os,re,uuid
from datetime import timedelta
import httpx
from fastapi import APIRouter,HTTPException,Request
from pydantic import Field,StrictBool,StrictInt
from sqlmodel import select
from .db import get_session
from .business_models import *
from .models import LeadCandidate
from .business_workflows import Strict,principal,uid,encode,digest,now,row_dict,exists,once,audit,check_actor
from .crm_adapter import DEFAULT_MAPPING,map_fields,validate_contract,CRMError,FeishuBitableAdapter,FIELD_TYPES
from .operations_workflows import customer_payload

router=APIRouter(prefix='/api/operations')

def config(s):return s.get(CRMDeliveryConfig,1) or CRMDeliveryConfig()

def add_event(s,kind,source,owner,payload,*,dedupe,depends_on=''):
    old=s.exec(select(DeliveryEvent).where(DeliveryEvent.dedupe_key==dedupe)).first()
    if old:return old
    cfg=config(s)
    row=DeliveryEvent(id=uid('delivery'),dedupe_key=dedupe,kind=kind,source_id=str(source),owner_actor_id=owner,
        payload_json=encode(payload),depends_on=depends_on,config_digest=cfg.digest,
        operation_token=str(uuid.uuid4()),status='pending' if cfg.enabled else 'blocked',error_code='' if cfg.enabled else 'connector_disabled')
    s.add(row);s.flush();return row

def public_event(row):return row_dict(row,{'payload_json','operation_token','dedupe_key'})

def safe_config(c):
    return {**row_dict(c,{'mapping_json','owner_mapping_json','field_types_json','credential_env'}),
        'mapping':json.loads(c.mapping_json),'bound_owner_count':len(json.loads(c.owner_mapping_json)),
        'field_types':json.loads(c.field_types_json),
        'owner_binding_ids':sorted(json.loads(c.owner_mapping_json)), 'credentials_stored':False,
        'supported_field_types':list(FIELD_TYPES),'automatic_dispatch_enabled':False}

class ConfigIn(Strict):
    mode:str=Field(pattern='^(unconfigured|local_mock|feishu)$')
    enabled:StrictBool=False
    app_token:str=Field(default='',max_length=100)
    table_id:str=Field(default='',max_length=100)
    mapping:dict[str,str]=Field(default_factory=lambda:dict(DEFAULT_MAPPING))
    owner_mapping:dict[str,str]=Field(default_factory=dict)
    field_types:dict[str,str]=Field(default_factory=dict)
    alert_owner_actor_id:str=Field(default='',max_length=100)
    destination_kind:str=Field(default='production',pattern='^(production|sandbox)$')
    acknowledge_sandbox_destination:StrictBool=False
    credential_source:str=Field(default='environment_token',pattern='^(environment_token|local_feishu_app)$')
    acknowledge_external_writes:StrictBool=False

@router.get('/delivery/config')
def get_config(request:Request):
    principal(request,{'admin','maintainer'})
    with get_session() as s:return safe_config(config(s))

@router.post('/delivery/config')
def save_config(body:ConfigIn,request:Request):
    p=principal(request,{'admin'})
    if body.mode=='unconfigured' and body.enabled:raise HTTPException(422,'未配置连接器不能启用')
    try:validate_contract(body.mapping,body.field_types)
    except CRMError:raise HTTPException(422,'字段映射需完整唯一；人员仅用于负责人，日期仅用于复核时间') from None
    if body.mode=='feishu':
        if not all(re.fullmatch(r'[A-Za-z0-9]{5,100}',x) for x in (body.app_token,body.table_id)):raise HTTPException(422,'多维表标识格式不正确')
        if body.enabled and not body.acknowledge_external_writes:raise HTTPException(422,'启用真实外发需人工确认')
        if body.destination_kind=='sandbox' and not body.acknowledge_sandbox_destination:
            raise HTTPException(422,'需管理员确认这是仅供合成联调的专用测试表，不代表平台自动认定隔离')
    if len(body.owner_mapping)>200 or any(not re.fullmatch(r'ou_[A-Za-z0-9_-]{5,100}',v) for v in body.owner_mapping.values()):raise HTTPException(422,'通知绑定必须是已授权的飞书 open_id')
    def update(s):
        for aid in body.owner_mapping:check_actor(s,aid)
        if body.alert_owner_actor_id:
            check_actor(s,body.alert_owner_actor_id,'maintainer')
            if body.alert_owner_actor_id not in body.owner_mapping:raise HTTPException(422,'告警接收人缺少通知身份绑定')
        c=config(s)
        for k in ('mode','enabled','app_token','table_id','credential_source','alert_owner_actor_id','destination_kind'):setattr(c,k,getattr(body,k))
        c.mapping_json=encode(body.mapping);c.owner_mapping_json=encode(body.owner_mapping)
        c.field_types_json=encode(body.field_types)
        c.digest=digest({k:v for k,v in body.model_dump().items() if k not in {'acknowledge_external_writes','acknowledge_sandbox_destination'}})
        c.updated_at=now();s.add(c);audit(s,p['id'],'delivery_configured',1,{'mode':c.mode,'enabled':c.enabled,'config_digest':c.digest})
        return safe_config(c)
    return once(request,'delivery-config',body,update)

class EnqueueIn(Strict):
    lead_id:StrictInt=Field(gt=0)
    version:StrictInt=Field(gt=0)
    digest:str=Field(pattern='^[a-f0-9]{64}$')
    owner_actor_id:str

@router.post('/delivery/leads')
def enqueue_lead(body:EnqueueIn,request:Request):
    p=principal(request,{'operator','sales'})
    def create(s):
        lead=exists(s,LeadCandidate,body.lead_id);check_actor(s,body.owner_actor_id,'sales')
        if lead.version!=body.version or lead.digest!=body.digest:raise HTTPException(409,'线索版本变化')
        payload=customer_payload(lead)
        owner=exists(s,TeamActor,body.owner_actor_id) if body.owner_actor_id!='local_owner' else None
        if not owner or lead.owner_label!=owner.label:raise HTTPException(409,'人工复核负责人标签与已授权销售人员不匹配')
        c=config(s)
        if c.mode=='feishu':check_destination_kind(c,lead.is_demo,owner)
        # Same external person key across accounts creates a single CRM event.
        record=add_event(s,'crm_record',str(lead.id),owner.id,
            {'lead_id':lead.id,'version':lead.version,'digest':lead.digest,'fields':payload,'is_demo':lead.is_demo},dedupe='crm:'+lead.external_key)
        if record.owner_actor_id!=owner.id:raise HTTPException(409,'已有入表任务负责人不同，需要人工核对而非重复入表')
        notice=add_event(s,'owner_notice',str(lead.id),owner.id,{'lead_id':lead.id,'summary':'一条已人工复核的咨询线索已入表，请在授权CRM查看。'},
            dedupe='owner:'+record.id,depends_on=record.id)
        audit(s,p['id'],'crm_enqueued',record.id,{'lead_id':lead.id,'notice_id':notice.id})
        return {'record':public_event(record),'notice':public_event(notice),'external_write_triggered':False}
    return once(request,'delivery-lead',body,create)

@router.get('/delivery')
def events(request:Request):
    p=principal(request,{'admin','operator','sales','maintainer'})
    with get_session() as s:
        q=select(DeliveryEvent)
        if not p['roles'].intersection({'admin','operator','maintainer'}):q=q.where(DeliveryEvent.owner_actor_id==p['id'])
        return {'items':[public_event(r) for r in s.exec(q.order_by(DeliveryEvent.created_at.desc()).limit(500)).all()]}

class DispatchIn(Strict):acknowledge_external_writes:StrictBool=False

class RemoteFailure(Exception):pass

def check_destination_kind(cfg,is_demo,owner):
    if cfg.destination_kind=='sandbox':
        if not is_demo or not owner or not owner.is_demo:raise HTTPException(409,'专用测试表仅允许演示线索与测试负责人')
    elif is_demo:raise HTTPException(409,'演示线索禁止进入真实CRM')

def token(http=None,source='environment_token'):
    from .crm_preflight import access_token
    try:return access_token(http,source)
    except CRMError as e:raise RemoteFailure(str(e)) from None

def send_remote(cfg,event):
    """Bounded single attempt. No proxy environment, redirects or token logging."""
    payload=json.loads(event.payload_json)
    with httpx.Client(timeout=httpx.Timeout(15,connect=5),follow_redirects=False,trust_env=False) as http:
        if event.kind=='crm_record':
            # Refuse a customer schema mismatch rather than silently stringify
            # a person/date/number into the wrong field type.
            from .crm_preflight import inspect_schema
            try:
                if not inspect_schema(cfg,http)['schema_ready']:raise RemoteFailure('field_type_mismatch')
            except CRMError as e:raise RemoteFailure(str(e)) from None
            try:
                fields=map_fields(payload['fields'],json.loads(cfg.mapping_json),
                    field_types=json.loads(cfg.field_types_json),owner_open_id=json.loads(cfg.owner_mapping_json).get(event.owner_actor_id))
                return FeishuBitableAdapter(cfg.app_token,cfg.table_id,lambda:token(http,cfg.credential_source),enabled=True,http=http).create(fields,event.operation_token)['record_id']
            except CRMError as exc:raise RemoteFailure(str(exc)) from None
        owners=json.loads(cfg.owner_mapping_json)
        if event.owner_actor_id not in owners:raise RemoteFailure('owner_binding_missing')
        try:
            response=http.post('https://open.feishu.cn/open-apis/im/v1/messages',params={'receive_id_type':'open_id'},
                headers={'Authorization':'Bearer '+token(http,cfg.credential_source)},json={'receive_id':owners[event.owner_actor_id],'msg_type':'text',
                    'content':encode({'text':payload.get('summary','Hub有一条待处理事项')}),'uuid':event.operation_token})
            if response.status_code>=500:raise RemoteFailure('submission_unknown')
            if response.status_code!=200:raise RemoteFailure('remote_rejected')
            data=response.json()
            if data.get('code')!=0:raise RemoteFailure('remote_rejected')
            mid=data['data']['message_id']
            if not isinstance(mid,str) or not mid:raise ValueError()
            return mid
        except (httpx.TransportError,ValueError,TypeError,KeyError):raise RemoteFailure('submission_unknown') from None


def dispatch(eid,actor,ack=False):
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        e=exists(s,DeliveryEvent,eid);c=config(s)
        if e.status=='done':return public_event(e)
        if e.status in {'unknown','running','failed'}:raise HTTPException(409,'需先人工核对未知/失败状态，不自动重投')
        if not c.enabled or c.mode=='unconfigured':raise HTTPException(409,'连接器未启用，任务保留')
        if c.digest!=e.config_digest:raise HTTPException(409,'目标配置已变化，需明确重新绑定')
        check_actor(s,e.owner_actor_id,'sales' if e.kind in {'crm_record','owner_notice'} else 'maintainer' if e.kind=='incident_notice' else None)
        if e.kind=='incident_notice':
            snap=json.loads(e.payload_json);incident=s.get(OpsIncident,snap.get('incident_key',''))
            if (not c.alert_owner_actor_id or e.owner_actor_id!=c.alert_owner_actor_id or not incident
                    or incident.status!='open' or incident.first_seen.isoformat()!=snap.get('incident_first_seen')):
                raise HTTPException(409,'告警已解除、重开或接收人变化；不发送旧告警')
        if e.kind=='task_notice':
            task=exists(s,DistributionTask,e.source_id)
            if task.employee_id!=e.owner_actor_id:raise HTTPException(409,'任务已转派，旧接收人通知停止')
        if c.mode=='feishu' and not ack:raise HTTPException(422,'真实外发必须人工确认')
        if e.depends_on and exists(s,DeliveryEvent,e.depends_on).status!='done':raise HTTPException(409,'依赖的CRM记录尚未成功；不会先通知或重建记录')
        if e.kind=='crm_record':
            snap=json.loads(e.payload_json);lead=exists(s,LeadCandidate,snap['lead_id'])
            if lead.version!=snap['version'] or lead.digest!=snap['digest'] or customer_payload(lead)!=snap['fields']:raise HTTPException(409,'复核后线索或负责人变化，需人工处理')
            if c.mode=='feishu':check_destination_kind(c,lead.is_demo,s.get(TeamActor,e.owner_actor_id))
        if c.mode=='local_mock':
            old=s.exec(select(MockDeliveryRecord).where(MockDeliveryRecord.operation_token==e.operation_token)).first()
            m=old or MockDeliveryRecord(id=uid('mock_delivery'),operation_token=e.operation_token,kind=e.kind,payload_json=e.payload_json)
            s.add(m);e.external_id=m.id;e.status='done';e.attempts+=1;e.finished_at=now();e.error_code='';s.add(e)
            audit(s,actor,'delivery_mock_done',e.id);s.commit();return public_event(e)
        # Commit claim BEFORE any POST. Startup recovery of a running event is
        # unknown even when the remote committed successfully but reply was lost.
        e.status='running';e.attempts+=1;e.error_code='';s.add(e);audit(s,actor,'delivery_claimed',e.id);s.commit();s.refresh(e);s.refresh(c)
    try:external=send_remote(c,e);error=''
    except RemoteFailure as exc:external='';error=str(exc)
    except Exception:external='';error='submission_unknown'
    with get_session() as s:
        live=exists(s,DeliveryEvent,eid)
        if live.status!='running':raise HTTPException(409,'投递状态发生变化，需人工核对')
        live.status='unknown' if error=='submission_unknown' else 'failed' if error else 'done'
        live.error_code=error;live.external_id=external;live.finished_at=now();s.add(live)
        audit(s,actor,'delivery_'+live.status,eid,{'error_code':error});s.commit();return public_event(live)

@router.post('/delivery/{eid}/dispatch')
def dispatch_route(eid:str,body:DispatchIn,request:Request):
    p=principal(request,{'admin','maintainer'})
    return dispatch(eid,p['id'],body.acknowledge_external_writes)

class ResolveIn(Strict):
    action:str=Field(pattern='^(bind|retry_confirmed_not_applied|confirm_applied)$')
    reason:str=Field(min_length=1,max_length=1000)
    external_id:str=Field(default='',max_length=160)

@router.post('/delivery/{eid}/resolve')
def resolve(eid:str,body:ResolveIn,request:Request):
    p=principal(request,{'admin','maintainer'})
    if not body.reason.strip():raise HTTPException(422,'需要核对依据')
    def update(s):
        e=exists(s,DeliveryEvent,eid);c=config(s)
        if e.status in {'done','running'}:raise HTTPException(409,'运行中或已完成的任务不能重置')
        if body.action=='confirm_applied':
            if e.status!='unknown' or not body.external_id:raise HTTPException(422,'仅未知提交可凭远端记录编号确认')
            e.external_id=body.external_id;e.status='done';e.finished_at=now()
        else:
            if body.action=='bind' and (e.attempts or e.status not in {'blocked','pending'}):raise HTTPException(409,'已尝试外发的任务不能改变目标')
            if body.action=='retry_confirmed_not_applied' and e.status not in {'failed','unknown'}:raise HTTPException(409,'只有确认未落地的失败/未知提交可以重新待投')
            if body.action=='retry_confirmed_not_applied' and c.digest!=e.config_digest:raise HTTPException(409,'已尝试任务不能静默更换目标')
            e.config_digest=c.digest;e.status='pending' if c.enabled else 'blocked';e.error_code='' if c.enabled else 'connector_disabled';e.finished_at=None
        s.add(e);audit(s,p['id'],'delivery_resolved',eid,{'action':body.action,'reason':body.reason});return public_event(e)
    return once(request,'delivery-resolve:'+eid,body,update)

def recover():
    with get_session() as s:
        rows=s.exec(select(DeliveryEvent).where(DeliveryEvent.status=='running')).all()
        for e in rows:e.status='unknown';e.error_code='submission_unknown';s.add(e)
        s.commit();return len(rows)
