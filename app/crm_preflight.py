"""Read-only schema preflight and renewable app authentication for a named table.

No record reads/creates, schema edits, permission grants or message sends.
Official contract: Feishu bitable-v1/app-table-field/list.
"""
from __future__ import annotations
import os,time,re
import httpx
from fastapi import APIRouter,Request,HTTPException
from .crm_adapter import CRMError,DEFAULT_MAPPING,validate_contract,FIELD_TYPES
from .business_workflows import principal,encode,digest
from .db import get_session
from .models import AppSetting

router=APIRouter(prefix='/api/operations')
API='https://open.feishu.cn/open-apis'
STATE='crm:preflight:v1'


def access_token(http,source='environment_token'):
    if source=='environment_token':
        value=os.environ.get('CREATORHUB_CRM_ACCESS_TOKEN','')
        if not value or len(value)>4096 or any(ord(c)<33 for c in value):
            raise CRMError('crm_credential_missing')
        return value
    if source!='local_feishu_app':raise CRMError('crm_credential_source_invalid')
    from .feishu_drive import token
    from .video_backup import BackupError
    try:return token(http)[0]
    except BackupError:raise CRMError('crm_local_app_auth_unavailable') from None


def schema_fields(http,cfg):
    """Finite GET retries, bounded body/pages. Never inspect table records."""
    if not all(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9]{5,100}',v) for v in (cfg.app_token,cfg.table_id)):
        raise CRMError('crm_destination_invalid')
    result={};page='';seen=set()
    for _ in range(5):
        for attempt in range(2):
            try:
                with http.stream('GET',API+f'/bitable/v1/apps/{cfg.app_token}/tables/{cfg.table_id}/fields',
                        params={'page_size':100,**({'page_token':page} if page else {})},
                        headers={'Authorization':'Bearer '+access_token(http,cfg.credential_source)}) as response:
                    if response.status_code in {429,500,502,503,504} and attempt==0:
                        time.sleep(.4);continue
                    if response.status_code!=200:raise CRMError('crm_schema_permission_or_request_rejected')
                    raw=bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw)>1024*1024:raise CRMError('crm_schema_response_limit')
                    import json
                    body=json.loads(raw)
                if type(body) is not dict or type(body.get('code')) is not int or body['code']!=0:
                    raise CRMError('crm_schema_permission_or_request_rejected')
                data=body.get('data')
                if not isinstance(data,dict) or not isinstance(data.get('items'),list):raise ValueError()
                break
            except (httpx.TimeoutException,httpx.NetworkError):
                if attempt==0:time.sleep(.4);continue
                raise CRMError('crm_schema_read_unavailable') from None
            except (httpx.HTTPError,ValueError,TypeError):
                raise CRMError('crm_schema_response_invalid') from None
        for field in data['items']:
            if (not isinstance(field,dict) or not isinstance(field.get('field_name'),str)
                    or type(field.get('type')) is not int or field['field_name'] in result):
                raise CRMError('crm_schema_response_invalid')
            result[field['field_name']]=field['type']
        more=data.get('has_more',False)
        if type(more) is not bool:raise CRMError('crm_schema_response_invalid')
        if not more:return result
        page=data.get('page_token')
        if not isinstance(page,str) or not 0<len(page)<=4096 or page in seen:raise CRMError('crm_schema_pagination_invalid')
        seen.add(page)
    raise CRMError('crm_schema_page_limit')


def inspect_schema(cfg,http):
    import json
    mapping=json.loads(cfg.mapping_json)
    _, types=validate_contract(mapping,json.loads(cfg.field_types_json))
    fields=schema_fields(http,cfg)
    missing=[k for k,name in mapping.items() if name not in fields]
    wrong=[k for k,name in mapping.items() if name in fields and fields[name]!=FIELD_TYPES[types[k]]]
    return {'schema_ready':not missing and not wrong,'missing_fields':missing,
            'wrong_type_fields':wrong,'supported_field_types':list(FIELD_TYPES),
            'fields_checked':len(mapping),'table_records_read':0,'records_written':0,
            'notifications_sent':0,'write_permission_verified':False}


def check_config(cfg):
    import json
    from datetime import datetime,timezone
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'config_digest':cfg.digest,
            'mode':cfg.mode,'schema_ready':False,'external_write_triggered':False,
            'table_records_read':0,'records_written':0,'notifications_sent':0,
            'write_permission_verified':False,'automatic_enable':False}
    if cfg.mode!='feishu':return {**result,'status':'needs_table','error_code':'crm_real_table_not_configured'}
    try:
        with httpx.Client(timeout=httpx.Timeout(15,connect=5),trust_env=False,follow_redirects=False) as http:
            result.update(inspect_schema(cfg,http))
        return {**result,'status':'ready' if result['schema_ready'] else 'schema_mismatch'}
    except CRMError as error:return {**result,'status':'not_ready','error_code':str(error)}
    except Exception:return {**result,'status':'not_ready','error_code':'crm_preflight_failed'}


@router.post('/delivery/preflight')
def preflight(request:Request):
    principal(request,{'admin','maintainer'})
    from .delivery_workflows import config
    from .wecom_workflows import _owner,_digest,_read,_write
    _owner(request)  # connector authentication is local-owner only
    identity=_digest(request.headers.get('Idempotency-Key',''))
    receipt_key=STATE+':request:'+identity
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        # Detach a value snapshot BEFORE commit expires ORM attributes.
        from .business_models import CRMDeliveryConfig
        cfg=CRMDeliveryConfig(**config(s).model_dump())
        old=_read(s,receipt_key)
        if old:
            if old.get('config_digest')!=cfg.digest:raise HTTPException(409,'crm_preflight_request_config_changed')
            return {**old,'replayed':True}
        gate=_read(s,STATE+':gate',{'next_allowed':0})
        if time.time()<gate['next_allowed']:raise HTTPException(429,'crm_preflight_cooldown')
        _write(s,STATE+':gate',{'next_allowed':time.time()+60,'identity':identity})
        _write(s,receipt_key,{'status':'running','config_digest':cfg.digest,'external_write_triggered':False})
        s.commit()
    result=check_config(cfg)  # no SQLite writer lock across network I/O
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        if config(s).digest!=cfg.digest:
            result.update(status='config_changed',schema_ready=False,error_code='crm_config_changed')
        _write(s,receipt_key,result)
        gate=_read(s,STATE+':gate')
        if gate and gate.get('identity')==identity:_write(s,STATE,result)
        s.commit()
    return result


@router.get('/delivery/preflight')
def status(request:Request):
    principal(request,{'admin','maintainer'})
    import json
    from .delivery_workflows import config
    with get_session() as s:
        row=s.get(AppSetting,STATE);c=config(s)
        result=json.loads(row.value) if row else None
        return {'last_check':result,'configuration_matches':bool(result and result.get('config_digest')==c.digest),
                'external_io_triggered':False,'write_permission_verified':False}
