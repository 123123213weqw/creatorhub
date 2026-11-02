"""Fixed v9 integration commands. No grant, enable, send or publish approval."""
from .client import field,REQUEST_KEY,require
from .operations import scrub

metric=field('object',properties={'value':field(['integer','null'],minimum=0,maximum=10**12),
    'reason':field('string',maxLength=200)},required=['value'],additionalProperties=False)
comment=field('object',properties={
    'comment_id':field('string',maxLength=160),'text':field('string',maxLength=2000),
    'nickname':field('string',maxLength=100),'author_ref':field('string',maxLength=160),
    'commented_at':field('string',maxLength=80)},required=['comment_id','text','commented_at'],additionalProperties=False)
CATALOG={
 'observations.list':({'account_id':field('integer',minimum=1)},[]),
 'observations.show':({'observation_id':field('string',pattern=r'^observation_[a-f0-9]{32}$')},['observation_id']),
 'observations.import':({'request_key':REQUEST_KEY,'account_id':field('integer',minimum=1),
    'work_id':field('integer',minimum=0),'item_id':field('string',maxLength=160),
    'source_url':field('string',maxLength=2000),'source_kind':field('string',enum=['manual_backend','authorized_export']),
    'source_note':field('string',maxLength=500),'observed_at':field('string',maxLength=80),
    'metrics':field('object',properties={k:metric for k in ('views','likes','comments','shares','favorites')},
        required=['views','likes','comments','shares','favorites'],additionalProperties=False),
    'comments':field('array',items=comment,maxItems=100),'comments_complete':field('boolean'),
    'is_demo':field('boolean'),'acknowledge_authorized_source':field('boolean')},
    ['request_key','account_id','item_id','source_url','source_kind','source_note','observed_at','metrics','acknowledge_authorized_source']),
 'crm.preflight':({'request_key':REQUEST_KEY},['request_key']),
 'crm.preflight-status':({},[]),
 'wecom.business-status':({},[]),
 'wecom.business-request':({'request_key':REQUEST_KEY},['request_key']),
 'wecom.business-refresh':({'request_key':REQUEST_KEY,'start_time':field('integer',minimum=1),
    'end_time':field('integer',minimum=1),'acknowledge_business_records':field('boolean')},
    ['request_key','start_time','end_time','acknowledge_business_records']),
 'operations.handoff':({},[]),'operations.profile':({},[]),'backup.policy':({},[]),
}
WRITES={'observations.import','crm.preflight','wecom.business-refresh'}
OUTPUT_KEYS=set('''protocol status checked_at config_digest mode schema_ready missing_fields wrong_type_fields
 supported_field_types fields_checked table_records_read records_written notifications_sent write_permission_verified
 automatic_enable external_write_triggered error_code replayed last_check configuration_matches external_io_triggered
 snapshot created leads_created leads_updated human_lead_review_required items more_may_exist platform_refresh_triggered
 id account_id platform work_id item_id source_kind observed_at imported_at imported_by metrics comments_complete is_demo
 content_digest imported_comment_count source_url_available value reason views likes comments shares favorites
 last_attempt last_complete cache_present cache_fresh refresh started finished summary window start_time end_time
 visible_member_count raw_ids_saved customer_details_read chat_content_read external_writes automatic_polling_enabled
 sections groups customer_messages group_messages moments observed_count tasks_inspected member_tasks member_tasks_sent
 recipient_results_observed enterprise_records personal_records enterprise_tasks_inspected member_published member_pending
 complete api_read_verified positive_sample_verified status_counts recipient_status_counts api_code
 read_count reply_count sales_count evidence_scope next_allowed_at minimum_interval_seconds
 profile configured automatic_schedule_enabled retention_deletion_enabled employee_mobile_acceptance_required
 employee_exclusion_reason required_platforms metrics_max_age_seconds backup_rpo_seconds backup_rto_seconds
 backup_retention_days independent_destination_note maintainer_label assets_access_note acceptance_note
 generated_at employee_mobile_acceptance facts pending account_count persona_count real_observation_snapshots
 verified_manual_publications crm_schema_preflight_ready crm_real_delivery_enabled backup_configured
 successful_restore_available wecom_business_complete_snapshot_available wecom_business_positive_samples
 real_crm_table_schema real_crm_write_and_owner_notice_acceptance independent_backup_destination_and_recovery_acceptance
 backup_targets required_platforms_confirmation metrics_cadence_confirmation existing_sop_persona_assets designated_maintainer
 long_running_and_customer_signoff customer_acceptance_complete not_claimed plan_only interval_seconds retention_days latest_success_at next_due_at backup_due blocked_reasons history_complete successful_snapshots retention_candidate_count protected_newest_snapshots independent_backup_accepted backup_interval_seconds 0 1 2 3'''.split())


def call(client,command,args):
    if command=='observations.import':
        require(args['acknowledge_authorized_source'] is True,'source_approval_required','需明确确认离线来源已获授权',3)
        raw=client._post('/api/operations/observations',{k:v for k,v in args.items() if k!='request_key'},args['request_key'])
    elif command=='crm.preflight':
        raw=client._post('/api/operations/delivery/preflight',{},args['request_key'])
    elif command=='wecom.business-refresh':
        require(args['acknowledge_business_records'] is True,'wecom_acknowledgement_required','需明确确认单一测试成员业务记录只读范围',3)
        require(0<args['end_time']-args['start_time']<=7*86400,'invalid_window','查询窗口须为1秒至7天',3)
        raw=client._post('/api/integrations/wecom/business/refresh',{k:v for k,v in args.items() if k!='request_key'},args['request_key'])
    else:
        paths={'observations.list':'/api/operations/observations','crm.preflight-status':'/api/operations/delivery/preflight',
            'wecom.business-status':'/api/integrations/wecom/business/status','operations.handoff':'/api/operations/handoff',
            'operations.profile':'/api/operations/delivery-profile','backup.policy':'/api/operations/backup-policy'}
        path=paths.get(command)
        if command=='observations.show':path='/api/operations/observations/'+args['observation_id']
        if command=='wecom.business-request':path='/api/integrations/wecom/business/requests/'+args['request_key']
        raw=client._get(path,{'account_id':args['account_id']} if command=='observations.list' and 'account_id' in args else None)
    require(type(raw) is dict,'invalid_response','Hub扩展响应格式不正确',5)
    require(raw.get('external_write_triggered',False) is False,'invalid_response','只读契约冲突，不能认定操作安全完成',5)
    if command.startswith('wecom.business-'):
        require(raw.get('protocol')=='1.0' and raw.get('raw_ids_saved') is False
            and raw.get('automatic_polling_enabled') is False,'invalid_response','企微业务只读契约不正确',5)
    result=scrub(raw)
    # Strip any raw fields even if an older or mismatched server returns them.
    def safe(v):
        forbidden={'comments_json','comments','comment_text','text','nickname','author_ref','source_note','source_url',
                   'external_userid','chat_id','msgid','moment_id','userid','body_hash','app_token','table_id'}
        if isinstance(v,dict):return {k:safe(x) for k,x in v.items() if k in OUTPUT_KEYS and (k not in forbidden or k=='comments' and isinstance(x,dict))}
        if isinstance(v,list):return [safe(x) for x in v]
        return v
    return {'result':safe(result),'state_authority':'existing_creatorhub','external_write_triggered':False}
