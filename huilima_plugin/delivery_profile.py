"""Customer handoff inputs and measurable backup-drill targets; no scheduler.

Configuration is not acceptance. No retention deletion, grants, publication or
connector enablement is implied by saving this profile.
"""
from __future__ import annotations
import json
from fastapi import APIRouter,Request,HTTPException
from pydantic import Field,StrictBool,StrictInt
from sqlmodel import select
from datetime import timedelta
from .business_workflows import Strict,principal,once,audit,encode,now
from .hub_adapter import AppSetting, VideoBackupConfig, VideoBackupOperation, DouyinAccount
from .business_models import CRMDeliveryConfig,AccountPersona,ObservationSnapshot,ManualPublishRecord
from .hub_adapter import get_session

router=APIRouter(prefix='/api/operations')
KEY='delivery:profile:v1'


class ProfileIn(Strict):
    employee_mobile_acceptance_required:StrictBool=True
    employee_exclusion_reason:str=Field(default='',max_length=300)
    required_platforms:list[str]=Field(default_factory=list,max_length=5)
    metrics_max_age_seconds:StrictInt|None=Field(default=None,ge=900,le=30*86400)
    backup_rpo_seconds:StrictInt|None=Field(default=None,ge=60,le=30*86400)
    backup_rto_seconds:StrictInt|None=Field(default=None,ge=1,le=7*86400)
    backup_retention_days:StrictInt|None=Field(default=None,ge=1,le=3650)
    backup_interval_seconds:StrictInt|None=Field(default=None,ge=3600,le=30*86400)
    independent_destination_note:str=Field(default='',max_length=500)
    maintainer_label:str=Field(default='',max_length=80)
    assets_access_note:str=Field(default='',max_length=1000)
    acceptance_note:str=Field(default='',max_length=1000)


def profile(s):
    row=s.get(AppSetting,KEY)
    return ProfileIn.model_validate(json.loads(row.value)).model_dump() if row else ProfileIn().model_dump()


def drill_targets(result,snapshot_created,started_at,elapsed):
    with get_session() as s:p=profile(s)
    age=max(0,(started_at-snapshot_created).total_seconds())
    result.update(snapshot_age_at_drill_seconds=round(age,3),
        rpo_target_seconds=p['backup_rpo_seconds'],rto_target_seconds=p['backup_rto_seconds'],
        rpo_target_evaluated=p['backup_rpo_seconds'] is not None,
        rto_target_evaluated=p['backup_rto_seconds'] is not None,
        snapshot_age_target_met=None if p['backup_rpo_seconds'] is None else age<=p['backup_rpo_seconds'],
        isolated_restore_duration_target_met=None if p['backup_rto_seconds'] is None else elapsed<=p['backup_rto_seconds'],
        rpo_measurement_scope='snapshot_age_at_drill_not_proven_business_data_loss',
        rto_measurement_scope='isolated_file_restore_not_full_service_disaster_recovery',
        independent_backup_accepted=False,customer_acceptance_complete=False)


@router.get('/delivery-profile')
def get_profile(request:Request):
    principal(request,{'admin','operator','maintainer'})
    with get_session() as s:return {'profile':profile(s),'configured':s.get(AppSetting,KEY) is not None,
        'automatic_schedule_enabled':False,'retention_deletion_enabled':False,'external_io_triggered':False}


@router.post('/delivery-profile')
def save_profile(body:ProfileIn,request:Request):
    from .wecom_workflows import _owner
    _owner(request);p=principal(request,{'admin'})
    if (len(set(body.required_platforms))!=len(body.required_platforms)
            or set(body.required_platforms)-{'douyin','xhs','shipinhao','kuaishou','tiktok'}):
        raise HTTPException(422,'delivery_platforms_invalid')
    if not body.employee_mobile_acceptance_required and not body.employee_exclusion_reason.strip():
        raise HTTPException(422,'delivery_employee_exclusion_reason_required')
    def update(s):
        row=s.get(AppSetting,KEY) or AppSetting(key=KEY);row.value=encode(body.model_dump());s.add(row)
        audit(s,p['id'],'delivery_profile_updated',KEY,{'employee_mobile_acceptance_required':body.employee_mobile_acceptance_required})
        return {'profile':body.model_dump(),'automatic_schedule_enabled':False,
                'retention_deletion_enabled':False,'customer_acceptance_complete':False}
    return once(request,'delivery-profile',body,update)


def handoff():
    from . import wecom_workflows as w
    from .crm_preflight import STATE as CRM_STATE
    from .wecom_business_workflows import STATE as BUSINESS_STATE
    with get_session() as s:
        p=profile(s);c=s.get(CRMDeliveryConfig,1);b=s.get(VideoBackupConfig,1)
        pre=s.get(AppSetting,CRM_STATE);pre=json.loads(pre.value) if pre else None
        business=s.get(AppSetting,BUSINESS_STATE);business=json.loads(business.value) if business else None
        observations=s.exec(select(ObservationSnapshot).where(ObservationSnapshot.is_demo==False)
            .order_by(ObservationSnapshot.observed_at.desc()).limit(101)).all()
        verified=s.exec(select(ManualPublishRecord).where(ManualPublishRecord.status=='verified').limit(1001)).all()
        personas=s.exec(select(AccountPersona)).all()
        backups=s.exec(select(VideoBackupOperation).where(VideoBackupOperation.kind=='restore',VideoBackupOperation.status=='done')
            .order_by(VideoBackupOperation.created_at.desc()).limit(1)).all()
        accounts=s.exec(select(DouyinAccount)).all()
        prepared=bool(pre and c and pre.get('schema_ready') and pre.get('config_digest')==c.digest)
        successful_business=business.get('last_complete') if business else None
        return {'protocol':'1.0','generated_at':now().isoformat()+'Z','employee_mobile_acceptance':
                    'required' if p['employee_mobile_acceptance_required'] else 'excluded_by_owner',
            'employee_exclusion_reason':p['employee_exclusion_reason'],
            'facts':{'account_count':len(accounts),'persona_count':len(personas),'real_observation_snapshots':len(observations),
                'verified_manual_publications':len(verified),'crm_schema_preflight_ready':prepared,
                'crm_real_delivery_enabled':bool(c and c.mode=='feishu' and c.enabled),
                'backup_configured':bool(b and b.provider!='unconfigured'),'successful_restore_available':bool(backups),
                'wecom_business_complete_snapshot_available':bool(successful_business)},
            'pending':{
                'wecom_business_positive_samples':not bool(successful_business and all(x.get('positive_sample_verified') for x in successful_business['summary']['sections'].values())),
                'real_crm_table_schema':not prepared,
                'real_crm_write_and_owner_notice_acceptance':True,
                'independent_backup_destination_and_recovery_acceptance':True,
                'backup_targets':any(p[k] is None for k in ('backup_rpo_seconds','backup_rto_seconds','backup_retention_days')),
                'required_platforms_confirmation':not bool(p['required_platforms']),
                'metrics_cadence_confirmation':p['metrics_max_age_seconds'] is None,
                'existing_sop_persona_assets':not bool(p['assets_access_note']),
                'designated_maintainer':not bool(p['maintainer_label']),
                'long_running_and_customer_signoff':True},
            'customer_acceptance_complete':False,'platform_refresh_triggered':False,'external_write_triggered':False,
            'not_claimed':['all_platforms_automatic','all_chats_collected','sent_equals_read','test_equals_customer_signoff']}


@router.get('/handoff')
def get_handoff(request:Request):
    principal(request,{'admin','operator','maintainer'})
    return handoff()


@router.get('/backup-policy')
def backup_policy(request:Request):
    """Local DB-only planning. Never touches a provider, deletes, or starts IO."""
    principal(request,{'admin','maintainer'})
    with get_session() as s:
        p=profile(s);c=s.get(VideoBackupConfig,1);stamp=now()
        rows=[]
        if c and c.provider!='unconfigured':
            rows=s.exec(select(VideoBackupOperation).where(VideoBackupOperation.kind=='snapshot',
                VideoBackupOperation.status=='done',VideoBackupOperation.provider==c.provider,
                VideoBackupOperation.store_id==c.store_id,VideoBackupOperation.root==c.root)
                .order_by(VideoBackupOperation.created_at.desc()).limit(501)).all()
        complete=len(rows)<=500;rows=rows[:500]
        # A completed row without a manifest is not a successful backup.
        valid=[r for r in rows if r.manifest_sha256 and r.snapshot_id and r.finished_at]
        latest=max((r.finished_at for r in valid),default=None)
        interval=p['backup_interval_seconds'];due=None if interval is None else (latest+timedelta(seconds=interval) if latest else stamp)
        blocked=[]
        if not c or c.provider=='unconfigured':blocked.append('backup_destination_unconfigured')
        if c and c.provider=='feishu_drive' and not c.cloud_upload_authorized:blocked.append('business_cloud_upload_not_authorized')
        if interval is None:blocked.append('backup_interval_unconfirmed')
        if not complete:blocked.append('backup_history_truncated')
        if latest and latest>stamp:blocked.append('backup_timestamp_in_future')
        retention=p['backup_retention_days']
        eligible=[]
        # Plan only; never propose pruning the newest two successful snapshots.
        if complete and retention is not None:
            candidates=sorted(valid,key=lambda r:r.finished_at,reverse=True)[2:]
            eligible=[r.snapshot_id for r in candidates if r.finished_at<stamp-timedelta(days=retention)]
        return {'protocol':'1.0','generated_at':stamp.isoformat()+'Z','plan_only':True,
            'interval_seconds':interval,'retention_days':retention,
            'latest_success_at':latest.isoformat()+'Z' if latest else None,
            'next_due_at':due.isoformat()+'Z' if due else None,
            'backup_due':None if interval is None or 'backup_timestamp_in_future' in blocked else due<=stamp,
            'blocked_reasons':blocked,'history_complete':complete,'successful_snapshots':len(valid),
            'retention_candidate_count':len(eligible),'protected_newest_snapshots':min(2,len(valid)),
            'independent_backup_accepted':False,'automatic_schedule_enabled':False,
            'retention_deletion_enabled':False,'external_io_triggered':False,'platform_refresh_triggered':False}
