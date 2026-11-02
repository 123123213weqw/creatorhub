"""Durable manual business-history refresh. Shared pacing with the v8 probe."""
from __future__ import annotations
import hashlib,re,time
from fastapi import APIRouter,HTTPException,Request
from pydantic import StrictBool,StrictInt,Field
from .business_workflows import Strict,encode
from .hub_adapter import get_session
from .wecom_readonly import load_credentials,WeComError
from .wecom_business_readonly import WeComBusinessReadOnly
from . import wecom_workflows as base

router=APIRouter(prefix='/api/integrations/wecom/business')
STATE='wecom:business:v1'
RECEIPT='wecom:business:request:'
SECTIONS={'groups','customer_messages','group_messages','moments'}
COUNTS={'observed_count','tasks_inspected','member_tasks','member_tasks_sent','recipient_results_observed',
        'enterprise_records','personal_records','enterprise_tasks_inspected','member_published','member_pending'}


class RefreshIn(Strict):
    start_time:StrictInt=Field(gt=0)
    end_time:StrictInt=Field(gt=0)
    acknowledge_business_records:StrictBool


def factory():return WeComBusinessReadOnly(load_credentials())


def project(report,body):
    if (not isinstance(report,dict) or type(report.get('visible_member_count')) is not int or report.get('visible_member_count')!=1
            or report.get('raw_ids_saved') is not False or report.get('customer_details_read') is not False
            or report.get('chat_content_read') is not False or type(report.get('external_writes')) is not int
            or report['external_writes']!=0 or not isinstance(report.get('sections'),dict)
            or set(report['sections'])!=SECTIONS):raise WeComError('wecom_business_summary_invalid')
    out={'window':{'start_time':body.start_time,'end_time':body.end_time},'sections':{},
         'visible_member_count':1,'raw_ids_saved':False,'chat_content_read':False,
         'customer_details_read':False,'external_writes':0,'automatic_polling_enabled':False}
    for name,section in report['sections'].items():
        if not isinstance(section,dict) or section.get('status') not in {'ok','unavailable'}:
            raise WeComError('wecom_business_summary_invalid')
        good=section['status']=='ok'
        value={'status':section['status'],'observed_count':None,'complete':False,
               'api_read_verified':good,'positive_sample_verified':False}
        if good:
            if type(section.get('complete')) is not bool:raise WeComError('wecom_business_summary_invalid')
            for k in COUNTS & set(section):
                v=section[k]
                if type(v) is not int or not 0<=v<=1000000:raise WeComError('wecom_business_summary_invalid')
                value[k]=v
            if value['observed_count'] is None:raise WeComError('wecom_business_summary_invalid')
            value.update(complete=section['complete'],positive_sample_verified=value['observed_count']>0)
            for k in ('status_counts','recipient_status_counts'):
                if k in section:
                    v=section[k]
                    if (type(v) is not dict or set(v)!={'0','1','2','3'}
                            or any(type(n) is not int or not 0<=n<=1000000 for n in v.values())):
                        raise WeComError('wecom_business_summary_invalid')
                    value[k]=v
        else:
            code=section.get('error_code')
            value['error_code']=code if isinstance(code,str) and re.fullmatch(r'wecom_[a-z0-9_]{1,80}',code) else 'wecom_business_read_unavailable'
            value['api_code']=section.get('api_code') if type(section.get('api_code')) is int else None
        out['sections'][name]=value
    out['read_count']=out['reply_count']=out['sales_count']=None
    out['status']='ok' if all(v['status']=='ok' and v['complete'] for v in out['sections'].values()) else 'partial'
    out['evidence_scope']='documented execution records only; never proof of customer reads, replies or sales'
    return out


def state(s):
    value=base._read(s,STATE,{'last_attempt':None,'last_complete':None})
    if set(value)!={'last_attempt','last_complete'}:raise HTTPException(409,'wecom_business_cache_invalid')
    return value


def public_record(record,now):
    if record is None:return None
    status=record['status']
    if status=='running' and now-record['started']>=base.MIN_INTERVAL:status='interrupted'
    return {k:v for k,v in record.items() if k not in {'body_hash'}} | {'status':status}


def view(s,receipt=None,replayed=False,triggered=False):
    now=base._clock();cache=state(s);shared=base._state(s)
    return {'protocol':'1.0','last_attempt':public_record(cache['last_attempt'],now),
        'last_complete':public_record(cache['last_complete'],now),
        'cache_present':cache['last_complete'] is not None,
        'cache_fresh':bool(cache['last_complete'] and 0<=now-cache['last_complete']['finished']<base.CACHE_TTL),
        'refresh':public_record(receipt,now),'replayed':replayed,'platform_refresh_triggered':triggered,
        'next_allowed_at':base._iso(shared['next_allowed']) if shared['next_allowed'] else None,
        'minimum_interval_seconds':base.MIN_INTERVAL,'automatic_polling_enabled':False,
        'external_write_triggered':False,'raw_ids_saved':False}


def refresh(key,body):
    if (body.acknowledge_business_records is not True or body.end_time-body.start_time>7*86400
            or body.start_time>=body.end_time or body.end_time>int(time.time())+300):
        raise HTTPException(422,'wecom_business_approval_or_window_invalid')
    identity=base._digest(key);body_hash=hashlib.sha256(encode(body.model_dump()).encode()).hexdigest()
    now=base._clock()
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        old=base._read(s,RECEIPT+identity)
        if old:
            if old.get('body_hash')!=body_hash:raise HTTPException(409,'wecom_business_request_key_conflict')
            return view(s,old,True)
        shared=base._state(s)
        if now<shared['next_allowed']:raise HTTPException(429,'wecom_refresh_cooldown')
        shared['next_allowed']=now+base.MIN_INTERVAL;base._write(s,base.STATE,shared)
        record={'status':'running','started':now,'body_hash':body_hash}
        cache=state(s);cache['last_attempt']=record;base._write(s,STATE,cache)
        base._write(s,RECEIPT+identity,record);s.commit()
    api=None;triggered=False
    try:
        api=factory();triggered=True
        report=project(api.inspect(start_time=body.start_time,end_time=body.end_time,acknowledge_business_records=True),body)
        record.update(status='completed' if report['status']=='ok' else 'partial',summary=report)
    except WeComError as error:
        code=error.code if isinstance(error.code,str) and re.fullmatch(r'wecom_[a-z0-9_]{1,80}',error.code) else 'wecom_business_read_unavailable'
        record.update(status='failed',error_code=code,api_code=error.api_code if type(error.api_code) is int else None)
    except Exception:record.update(status='failed',error_code='wecom_business_read_failed')
    finally:
        if api:
            try:api.close()
            except Exception:pass
    record['finished']=base._clock()
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        cache=state(s)
        # A slow old result never replaces a more recent refresh.
        if cache['last_attempt'] and cache['last_attempt'].get('body_hash')==body_hash and cache['last_attempt'].get('started')==now:
            cache['last_attempt']=record
            if record['status']=='completed':cache['last_complete']=record
            base._write(s,STATE,cache)
        base._write(s,RECEIPT+identity,record);s.commit()
        return view(s,record,False,triggered)


@router.get('/status')
def status(request:Request):
    base._owner(request)
    with get_session() as s:return view(s)


@router.post('/refresh')
def refresh_route(body:RefreshIn,request:Request):
    base._owner(request)
    return refresh(request.headers.get('Idempotency-Key',''),body)


@router.get('/requests/{key}')
def receipt(key:str,request:Request):
    base._owner(request);identity=base._digest(key)
    with get_session() as s:
        old=base._read(s,RECEIPT+identity)
        if not old:raise HTTPException(404,'wecom_business_receipt_not_found')
        return view(s,old,True)
