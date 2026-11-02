"""Business workflows in the SAME Hub database; no credentials exported."""
from datetime import datetime
from typing import Optional
from sqlmodel import Field,SQLModel


class TeamActor(SQLModel,table=True):
    id:str=Field(primary_key=True)
    label:str
    roles_json:str='[]'
    enabled:bool=True
    is_demo:bool=False
    created_at:datetime=Field(default_factory=datetime.utcnow)


class TeamCredential(SQLModel,table=True):
    id:str=Field(primary_key=True)
    actor_id:str=Field(index=True)
    token_hash:str=Field(unique=True,index=True)
    enabled:bool=True
    expires_at:datetime
    created_at:datetime=Field(default_factory=datetime.utcnow)


class BusinessContent(SQLModel,table=True):
    id:str=Field(primary_key=True)
    label:str
    owner_actor_id:str
    created_at:datetime=Field(default_factory=datetime.utcnow)


class ContentVersion(SQLModel,table=True):
    id:str=Field(primary_key=True)
    content_id:str=Field(index=True)
    revision:int
    title:str
    description:str=''
    tags:str=''
    asset_ids_json:str
    assets_digest:str
    digest:str
    is_demo:bool=False
    created_at:datetime=Field(default_factory=datetime.utcnow)


class AccountPersona(SQLModel,table=True):
    account_id:int=Field(primary_key=True)
    alias:str=''
    persona:str=''
    owner_actor_id:str='local_owner'
    updated_at:datetime=Field(default_factory=datetime.utcnow)


class ContentVariant(SQLModel,table=True):
    id:str=Field(primary_key=True)
    version_id:str=Field(index=True)
    account_id:int
    platform:str
    title:str
    description:str=''
    topics:str=''
    visibility:str='private'
    digest:str
    created_at:datetime=Field(default_factory=datetime.utcnow)


class PublishPlan(SQLModel,table=True):
    id:str=Field(primary_key=True)
    label:str
    actor_id:str
    status:str='planned'
    created_at:datetime=Field(default_factory=datetime.utcnow)


class PublishPlanItem(SQLModel,table=True):
    id:str=Field(primary_key=True)
    plan_id:str=Field(index=True)
    variant_id:str
    status:str='planned'
    publish_task_id:Optional[int]=None
    error_code:str=''


class DistributionTask(SQLModel,table=True):
    id:str=Field(primary_key=True)
    version_id:str
    version_digest:str
    employee_id:str=Field(index=True)
    assigned_by:str
    due_at:datetime
    status:str='assigned'
    revision:int=1
    note:str=''
    created_at:datetime=Field(default_factory=datetime.utcnow)


class DistributionTarget(SQLModel,table=True):
    id:str=Field(primary_key=True)
    task_id:str=Field(index=True)
    channel:str
    label:str
    status:str='assigned'
    revision:int=1
    latest_proof_id:str=''
    last_note:str=''
    verified_at:Optional[datetime]=None


class DistributionProof(SQLModel,table=True):
    id:str=Field(primary_key=True)
    target_id:str=Field(index=True)
    task_id:str
    version_digest:str
    submitted_by:str
    file_path:str
    mime_type:str
    sha256:str=Field(index=True)
    size:int
    note:str
    duplicate_suspected:bool=False
    state:str='submitted'
    created_at:datetime=Field(default_factory=datetime.utcnow)


class ProofVerification(SQLModel,table=True):
    id:str=Field(primary_key=True)
    proof_id:str=Field(index=True)
    target_id:str
    reviewer_id:str
    decision:str
    reason:str
    evidence_scope:str='sent_only_not_read_or_sale'
    created_at:datetime=Field(default_factory=datetime.utcnow)


class ManualPublishRecord(SQLModel,table=True):
    id:str=Field(primary_key=True)
    variant_id:str
    account_id:int
    external_url:str
    published_at:datetime
    recorded_by:str
    note:str
    status:str='pending_review'
    reviewed_by:str=''
    review_note:str=''
    created_at:datetime=Field(default_factory=datetime.utcnow)


class CRMDeliveryConfig(SQLModel,table=True):
    id:int=Field(default=1,primary_key=True)
    mode:str='unconfigured'  # local_mock | feishu; only configured by administrator
    enabled:bool=False
    app_token:str=''
    table_id:str=''
    mapping_json:str='{}'
    owner_mapping_json:str='{}'
    credential_env:str='CREATORHUB_CRM_ACCESS_TOKEN'
    credential_source:str='environment_token'  # opt-in local_feishu_app; never store secrets
    field_types_json:str='{}'  # legacy mappings remain text unless explicitly changed
    alert_owner_actor_id:str=''  # opt-in recipient, no automatic alert dispatch
    destination_kind:str='production'  # explicit sandbox accepts only synthetic leads
    digest:str=''
    updated_at:datetime=Field(default_factory=datetime.utcnow)


class WeComMomentTask(SQLModel,table=True):
    """Immutable reviewed intent; a job receipt is NOT a published Moment."""
    id:str=Field(primary_key=True)
    label:str
    binding_id:str
    sender_user_id:str
    customer_tag_ids_json:str
    customer_scope_note:str
    text:str=''
    media_type:str='text'
    media_manifest_json:str='[]'
    digest:str
    demo_only:bool=False
    status:str=Field(default='draft',index=True)
    approved_digest:str=''
    approved_at:Optional[datetime]=None
    review_note:str=''
    approved_by:str=''
    submit_key_hash:str=''
    submit_body_hash:str=''
    phase:str='local_draft'
    task_creation_attempts:int=0
    media_upload_attempts:int=0
    uploads_json:str='[]'
    submitted_at:Optional[datetime]=None
    job_id:str=''
    job_received_at:Optional[datetime]=None
    moment_id:str=''
    creation_job_status:Optional[int]=None
    creation_scope_rejected:bool=False
    member_task_observed:bool=False
    member_published:Optional[bool]=None
    execution_checked_at:Optional[datetime]=None
    last_error_code:str=''
    last_api_code:Optional[int]=None
    query_nonce:str=''
    query_started_at:Optional[datetime]=None
    last_query_at:Optional[datetime]=None
    last_query_error:str=''
    last_query_api_code:Optional[int]=None
    created_at:datetime=Field(default_factory=datetime.utcnow)
    updated_at:datetime=Field(default_factory=datetime.utcnow)


class ObservationSnapshot(SQLModel,table=True):
    """Immutable, source-attributed manual/offline input; never a platform probe."""
    id:str=Field(primary_key=True)
    account_id:int=Field(index=True)
    platform:str
    work_id:int=0
    item_id:str
    source_url:str
    source_kind:str
    source_note:str
    observed_at:datetime
    imported_at:datetime=Field(default_factory=datetime.utcnow)
    imported_by:str
    metrics_json:str
    comments_json:str
    comments_complete:bool=False
    is_demo:bool=False
    content_digest:str=Field(unique=True,index=True)


class OfflineCommentCursor(SQLModel,table=True):
    source_key:str=Field(primary_key=True)
    observed_at:datetime
    snapshot_id:str


class DeliveryEvent(SQLModel,table=True):
    id:str=Field(primary_key=True)
    dedupe_key:str=Field(unique=True,index=True)
    kind:str  # crm_record | owner_notice | task_notice | incident_notice
    source_id:str
    owner_actor_id:str
    depends_on:str=''
    config_digest:str=''
    payload_json:str='{}'
    operation_token:str
    status:str='pending'  # blocked | running | done | failed | unknown
    attempts:int=0
    next_attempt_at:Optional[datetime]=None
    external_id:str=''
    error_code:str=''
    created_at:datetime=Field(default_factory=datetime.utcnow)
    finished_at:Optional[datetime]=None


class MockDeliveryRecord(SQLModel,table=True):
    id:str=Field(primary_key=True)
    operation_token:str=Field(unique=True,index=True)
    kind:str
    payload_json:str
    created_at:datetime=Field(default_factory=datetime.utcnow)


class OpsIncident(SQLModel,table=True):
    key:str=Field(primary_key=True)
    severity:str
    summary:str
    status:str='open'
    first_seen:datetime=Field(default_factory=datetime.utcnow)
    last_seen:datetime=Field(default_factory=datetime.utcnow)
    resolved_at:Optional[datetime]=None


class OpsCheck(SQLModel,table=True):
    id:str=Field(primary_key=True)
    result_json:str
    created_at:datetime=Field(default_factory=datetime.utcnow)


class BusinessAudit(SQLModel,table=True):
    id:str=Field(primary_key=True)
    actor_id:str
    action:str
    object_id:str
    detail_json:str='{}'
    created_at:datetime=Field(default_factory=datetime.utcnow,index=True)

__all__ = ['TeamActor','TeamCredential','BusinessContent','ContentVersion','AccountPersona','ContentVariant',
    'PublishPlan','PublishPlanItem','DistributionTask','DistributionTarget','DistributionProof','ProofVerification',
    'ManualPublishRecord','CRMDeliveryConfig','DeliveryEvent','MockDeliveryRecord','OpsIncident','OpsCheck','BusinessAudit',
    'ObservationSnapshot','OfflineCommentCursor']
