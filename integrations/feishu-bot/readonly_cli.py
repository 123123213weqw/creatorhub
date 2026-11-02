"""Pinned command allowlist, metadata projection and strict tool arguments.

This is NOT a shell tool, HTTP proxy, permission broker, or automatic updater.
Only schema declarations are derived from CLI; command exposure is explicit.
"""
import copy,json,math,re
from pathlib import Path
from datetime import datetime
from hub import HubError,TOOLS as LEGACY
CATALOG=json.loads((Path(__file__).parent/'readonly-catalog.json').read_text())
NEW_MAP={k:v['command'] for k,v in CATALOG.items()}
TOOLS=copy.deepcopy(LEGACY)+[{'type':'function','function':{'name':name,'description':spec['description'],
    'parameters':spec['parameters']}} for name,spec in CATALOG.items()]
for tool in TOOLS[:len(LEGACY)]:
    tool['function']['description']=tool['function']['description'].replace('当前实时总览','已保存总览')+' 只返回编号、状态和数量；不含正文、昵称和原始错误详情。'
SPECS={x['function']['name']:x['function']['parameters'] for x in TOOLS}


def validate(name,args):
    spec=SPECS.get(name)
    if spec is None:raise HubError('tool_not_allowed')
    if (type(args) is not dict or not set(spec.get('required',[]))<=set(args)
            or set(args)-set(spec['properties'])):raise HubError('invalid_arguments')
    def check(value,p):
        kind=p.get('type')
        if kind=='integer':ok=type(value) is int and p.get('minimum',1)<=value<=p.get('maximum',2**31-1)
        elif kind=='string':ok=(type(value) is str and len(value)<=p.get('maxLength',512)
            and ('enum' not in p or value in p['enum'])
            and ('pattern' not in p or re.fullmatch(p['pattern'],value) is not None))
        elif kind=='boolean':ok=type(value) is bool
        else:ok=False  # No free-form objects, arrays, URLs, code, or paths.
        if not ok:raise HubError('invalid_arguments')
    for key,value in args.items():check(value,spec['properties'][key])
    return args


def _number(value):
    return value is None or type(value) is bool or type(value) is int and abs(value)<=10**15 or type(value) is float and math.isfinite(value) and abs(value)<=10**15

# Key names are a reviewed subset. Unknown/free-form fields never reach cloud.
NAMES=set('''items platforms counts summary totals health result last_success last_attempt refresh sections
 groups customer_messages group_messages moments status_counts task_status_counts execution_status_counts
 checks readiness incidents profile policy inventory last_check source state_authority evidence_scope
 id account_id work_id job_id task_id operation_id snapshot_id platform queue_type kind status state phase
 media_type visibility mode target_kind provider crm_mode version protocol schema_version cli_version
 page page_size pages total total_in_snapshot more_may_exist generated_at read_at fetched_at checked_at
 created_at updated_at approved_at submitted_at last_query_at execution_checked_at started_at finished_at
 last_check_at verified_at next_allowed_at last_success_at last_attempt_at
 media_count customer_tag_count task_creation_attempts media_upload_attempts creation_job_status
 creation_scope_rejected member_task_observed member_published job_known moment_known demo_only is_demo
 requires_member_confirmation automatic_submit_enabled automatic_polling_enabled platform_refresh_triggered
 external_write_triggered external_io_triggered automatic_enable automatic_schedule_enabled retention_deletion_enabled
 review_required review_version has_read_login has_creator requires_creator_login local_assets_ready
 hub_reachable new_service_started write_extension_ready offline_leads_ready real_crm_enabled business_operations_ready
 team_gateway_enabled video_backup_ready wecom_readonly_ready wecom_moments_ready cache_present cache_fresh
 cache_age_seconds cache_ttl_seconds refresh_min_interval_seconds clock_skew_detected raw_customer_ids_saved
 customer_details_available chat_content_available chat_content_read binding_available binding_fresh
 write_permission_verified configured destination_available cloud_upload_authorized plan_only backup_due
 capacity_known capacity_bytes available_bytes total_bytes verified files_checked files_total files_count
 records_written notifications_sent fields_checked missing_fields_count wrong_type_fields_count
 schema_ready configuration_matches stale partial complete count observed_count verified_targets total_targets
 completed failed pending running blocked canceled uncertain accepted rejected draft approved published awaiting_member
 last_error_code last_api_code last_query_error last_query_api_code error_code api_code integrity digest
 read_count reply_count sales_count source_kind file_count asset_count work_count comment_count
 accounts monitors downloaded comments visible_member_count enabled_member_count customer_count
 current_member_only no_background_worker preview_only independent_database independent_queue independent_approval
 background_worker'''.split())
NAMES.update('''employee_id version_id variant_id content_id plan_id target_id revision due_at
 facts account_count persona_count real_observation_snapshots verified_manual_publications
 crm_schema_preflight_ready crm_real_delivery_enabled backup_configured successful_restore_available
 wecom_business_complete_snapshot_available wecom_business_positive_samples real_crm_table_schema
 real_crm_write_and_owner_notice_acceptance independent_backup_destination_and_recovery_acceptance
 backup_targets required_platforms_confirmation metrics_cadence_confirmation existing_sop_persona_assets
 designated_maintainer long_running_and_customer_signoff customer_acceptance_complete team_actors_bound
 remote_team_gateway_enabled crm_configured crm_enabled owner_notification_bindings independent_backup_accepted
 employee_mobile_acceptance_required human_approval_command employee_mobile_acceptance pending_input_count
 human_confirmation_count platform_write_triggered storage_io_only external_io cloud_file_limit_verified
 cloud_quota_available_bytes max_bytes independent_backup_verified video_files bytes skipped_files
 platform_fetch_triggered'''.split())
CONTAINERS=set('''items platforms counts summary totals health result last_success last_attempt refresh sections
 groups customer_messages group_messages moments status_counts task_status_counts execution_status_counts
 checks readiness incidents profile policy inventory last_check'''.split())
IDS=re.compile(r'(?:asset|wecom_moment|vbak|publish|sync|content|version|variant|plan|target|distribution|delivery|observation|actor)_[a-f0-9]{32}')
ENUMS=set('''all douyin xhs kuaishou shipinhao tiktok text images image video public private friends
 ok healthy unavailable unknown not_available unconfigured configured ready not_ready enabled disabled feishu
 feishu_drive local local_mock sandbox production none not_started not_checked running interrupted completed done
 failed rejected approved draft pending queued active blocked canceled cancelled doing publishing published
 awaiting_member creating submitting unknown partial success warning error stopped invalid stale fresh verified
 scope_rejected failed_before_task archived completed_with_errors pass passed fail failed_review
 local_draft local_approved checking_scope media_post_claimed task_post_claimed job_received_not_published
 task_created_not_published member_publication_verified_not_customer_read
 invalid_member_or_customer_tags_no_retry task_rejected task_outcome_requires_manual_check
 no_task_post_attempted async_creation_rejected restart_requires_original_task_check local_only_not_remote_recall
 existing_creatorhub existing_hub_summary_cache existing_hub_business_summary_cache cli_only source_unavailable
 enterprise_created member_created business_summary_readonly operation_count local_files mounted_directory
 pending_review recorded_mock candidate eligible not_required_out_of_scope'''.split())
DROP=object()


def project(value,key='',depth=0):
    if depth>10:return DROP
    if _number(value):return value
    if type(value) is str:
        if key in {'digest'}:return value if re.fullmatch(r'[a-f0-9]{64}',value) else DROP
        if key in {'id','task_id','operation_id','snapshot_id','job_id','employee_id','version_id','variant_id','content_id','plan_id','target_id'}:
            # Platform job IDs/customer IDs are not part of this model contract.
            return value if IDS.fullmatch(value) else DROP
        if key.endswith('_at') or key=='read_at':
            try:
                if not re.fullmatch(r'\d{4}-\d\d-\d\dT[0-9:.+-]+Z?',value):return DROP
                datetime.fromisoformat(value.replace('Z','+00:00'));return value
            except (ValueError,TypeError):return DROP
        if key in {'version','protocol','schema_version','cli_version'}:
            return value if re.fullmatch(r'\d{1,2}\.\d{1,2}(?:\.\d{1,3})?',value) else DROP
        if key in {'error_code','last_error_code','last_query_error'}:
            if not value:return ''
            return value if re.fullmatch(r'(?:wecom|hub|cli|backup|crm|schema|config|source|destination|storage|connection)_[a-z0-9_]{1,90}',value) else 'redacted_error'
        return value if value in ENUMS else DROP
    if type(value) is list:
        out=[]
        for v in value[:100]:
            item=project(v,key,depth+1)
            if item is not DROP:out.append(item)
        return out
    if type(value) is dict:
        out={}
        for name,v in value.items():
            if type(name) is not str:continue
            if name not in NAMES and not (key.endswith('_counts') and re.fullmatch('[0-4]',name)):
                continue
            item=project(v,name,depth+1)
            if item is not DROP:out[name]=item
        return out
    return DROP


def safe_metadata(raw):
    if type(raw) is not dict:raise HubError('cli_invalid_response')
    out=project(raw)
    if out is DROP:raise HubError('cli_invalid_response')
    def truncated(v,depth=0):
        if depth>10:return False
        if type(v) is list:return len(v)>100 or any(truncated(x,depth+1) for x in v[:100])
        if type(v) is dict:return any(truncated(x,depth+1) for k,x in v.items() if k in NAMES)
        return False
    if truncated(raw):out.update(projection_truncated=True,more_may_exist=True)
    return out
