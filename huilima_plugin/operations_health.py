"""Local-only health/overdue reporting. Never probes a platform or sends alerts."""
from __future__ import annotations
import asyncio,json,shutil
from pathlib import Path
from datetime import timedelta
from fastapi import APIRouter,Request
from sqlmodel import select
from .hub_adapter import get_session
from .business_models import *
from .business_workflows import principal,uid,encode,now,row_dict,task_dict

router=APIRouter(prefix='/api/operations')

def readiness():
    with get_session() as s:
        c=s.get(CRMDeliveryConfig,1)
        from .hub_adapter import VideoBackupConfig
        b=s.get(VideoBackupConfig,1)
        actors=s.exec(select(TeamActor).where(TeamActor.enabled==True,TeamActor.is_demo==False)).all()
        from .delivery_profile import profile
        delivery_profile=profile(s)
        return {'team_actors_bound':len(actors),'remote_team_gateway_enabled':False,
            'crm_configured':bool(c and c.mode=='feishu'),'crm_enabled':bool(c and c.mode=='feishu' and c.enabled),
            'owner_notification_bindings':len(json.loads(c.owner_mapping_json)) if c else 0,
            'backup_configured':bool(b and b.provider!='unconfigured'),
            'independent_backup_accepted':False,'customer_acceptance_complete':False,
            'human_confirmation_required':['publish','lead_review','proof_verification','manual_publish_verification','connector_enable','unknown_delivery_reconciliation'],
            'employee_mobile_acceptance_required':delivery_profile['employee_mobile_acceptance_required'],
            'pending_external_inputs':([
                '客户CRM表及字段类型/访问范围/销售负责人授权',
                '第二独立存储位置、保留期、RPO/RTO',
                '代表素材、账号人设、业务验收标准与真实发布单独确认',
                '既有SOP/豆包技能资产访问和长时间运行签认'] +
                (['真实员工与核验人名单、可信身份绑定及手机可访问的授权入口']
                 if delivery_profile['employee_mobile_acceptance_required'] else [])),
            'not_automatically_enabled':['real_CRM_delivery','real_employee_notifications','public_network_gateway','automatic_publication','platform_polling','cloud_backup']}


def run_check():
    from .hub_adapter import main_module
    main = main_module()
    observed={};facts={};stamp=now()
    root=Path(main.cfg.db_path).absolute().parent
    try:
        usage=shutil.disk_usage(root);facts['disk_free_bytes']=usage.free
        if usage.free<512*1024*1024:observed['storage:low']=('critical','Hub数据目录可用空间低于512MiB')
    except OSError:facts['disk_free_bytes']=None;observed['storage:unavailable']=('critical','Hub数据目录不可用，不能认定素材为零')
    with get_session() as s:
        check=s.connection().exec_driver_sql('PRAGMA quick_check(1)').scalar()
        facts['database_quick_check']=check=='ok'
        if check!='ok':observed['database:integrity']=('critical','本地业务数据库完整性检查失败')
        events=s.exec(select(DeliveryEvent).where(DeliveryEvent.status.in_(['failed','unknown'])).limit(1001)).all()
        for e in events:observed['delivery:'+e.id]=('critical' if e.status=='unknown' else 'warning','外部投递结果未知，需人工核对' if e.status=='unknown' else '外部投递失败，需处理配置或权限')
        tasks=s.exec(select(DistributionTask).where(DistributionTask.due_at<stamp,DistributionTask.status!='canceled').limit(1001)).all()
        overdue=0
        for t in tasks:
            targets=s.exec(select(DistributionTarget).where(DistributionTarget.task_id==t.id)).all()
            if any(x.status not in {'verified','canceled'} for x in targets):
                overdue+=1;observed['overdue:'+t.id]=('warning','分发任务有逾期未核验目标')
        facts.update(overdue_tasks=overdue,problem_deliveries=len(events),bounded_scan_truncated=len(tasks)>1000 or len(events)>1000)
        opened=0;resolved=0
        for key,(severity,summary) in observed.items():
            row=s.get(OpsIncident,key)
            new_incident=not row or row.status!='open'
            if not row:row=OpsIncident(key=key,severity=severity,summary=summary);opened+=1
            elif row.status!='open':opened+=1;row.first_seen=stamp
            row.status='open';row.last_seen=stamp;row.resolved_at=None;s.add(row)
            if new_incident and not key.startswith('delivery:'):
                from .delivery_workflows import add_event,config
                owner=config(s).alert_owner_actor_id or 'local_owner'
                add_event(s,'incident_notice',key,owner,{'summary':summary,'incident_key':key,
                    'incident_first_seen':row.first_seen.isoformat()},dedupe='incident:'+key+':'+stamp.isoformat())
        for row in s.exec(select(OpsIncident).where(OpsIncident.status=='open')).all():
            if row.key not in observed and not facts['bounded_scan_truncated']:
                row.status='resolved';row.resolved_at=stamp;resolved+=1;s.add(row)
        result={'checked_at':stamp.isoformat()+'Z','facts':facts,'open_incidents':len(observed),'new_or_reopened':opened,'resolved':resolved,
            'platform_requests':0,'external_notifications_sent':0,'automatic_dispatch':False}
        s.add(OpsCheck(id=uid('check'),result_json=encode(result),created_at=stamp))
        # Bounded operational history, not business/audit/evidence deletion.
        for old in s.exec(select(OpsCheck).where(OpsCheck.created_at<stamp-timedelta(days=30))).all():s.delete(old)
        s.commit();return result

@router.get('/readiness')
def get_readiness(request:Request):
    principal(request,{'admin','operator','maintainer'})
    return readiness()

@router.get('/health')
def health(request:Request):
    principal(request,{'admin','maintainer'})
    with get_session() as s:
        last=s.exec(select(OpsCheck).order_by(OpsCheck.created_at.desc())).first()
        return {'last_check':json.loads(last.result_json) if last else None,'last_check_fresh':bool(last and now()-last.created_at<timedelta(seconds=180)),
            'incidents':[row_dict(r) for r in s.exec(select(OpsIncident).order_by(OpsIncident.last_seen.desc()).limit(200)).all()],
            'checks_are_local_only':True,'check_interval_seconds':60,'external_alert_transport_enabled':False}

@router.post('/health/check')
def manual_check(request:Request):
    principal(request,{'admin','maintainer'})
    return run_check()

@router.get('/reports')
def report(request:Request):
    p=principal(request,{'operator','employee','reviewer','maintainer'})
    with get_session() as s:
        q=select(DistributionTask)
        if not p['roles'].intersection({'admin','operator','reviewer','maintainer'}):q=q.where(DistributionTask.employee_id==p['id'])
        tasks=s.exec(q.order_by(DistributionTask.created_at.desc()).limit(1001)).all();items=[task_dict(s,t,p) for t in tasks[:1000]]
        employees={}
        for t in items:
            e=employees.setdefault(t['employee_id'],{'employee_id':t['employee_id'],'tasks':0,'targets':0,'verified':0,'canceled':0,'overdue_tasks':0})
            e['tasks']+=1;e['targets']+=t['total_targets'];e['verified']+=t['verified_targets'];e['canceled']+=t['canceled_targets'];e['overdue_tasks']+=int(t['overdue'])
        return {'generated_at':now().isoformat()+'Z','items':list(employees.values()),'more_may_exist':len(tasks)>1000,
            'evidence_scope':'human-reviewed send evidence only; not customer read, qualified lead or sale',
            'customer_reads':None,'sales':None,'unknown_reason':'no authorized read/transaction data source'}

async def monitor_loop():
    from starlette.concurrency import run_in_threadpool
    while True:
        try:await run_in_threadpool(run_check)
        except asyncio.CancelledError:raise
        except Exception:
            # Do not log DB/contact bodies or exception strings with secrets.
            import logging
            logging.getLogger(__name__).error('Local operations check failed; health is not confirmed')
        await asyncio.sleep(60)
