"""Portable, private business snapshot. No credentials or executable role grants.

Recovery is an isolated inspection package, never an automatic DB import. Team
identities must be re-authorized and external delivery reconciled by a human.
"""
import json
from pathlib import Path
from sqlmodel import select
from .db import get_session
from .business_models import *
from .business_workflows import row_dict

MODELS={
 'actors':(TeamActor,{'roles_json'}),'contents':(BusinessContent,set()),'versions':(ContentVersion,set()),
 'personas':(AccountPersona,set()),'variants':(ContentVariant,set()),'plans':(PublishPlan,set()),'plan_items':(PublishPlanItem,set()),
 'tasks':(DistributionTask,set()),'targets':(DistributionTarget,set()),'proofs':(DistributionProof,{'file_path'}),
 'verifications':(ProofVerification,set()),'manual_publishes':(ManualPublishRecord,set()),
 'delivery_receipts':(DeliveryEvent,{'payload_json','operation_token','config_digest','dedupe_key'}),
 'audit_index':(BusinessAudit,{'detail_json'}),
}
LEGACY_TABLES=set(MODELS)
# Backward-readable v7/v8 packages. Raw imported comments are intentionally not
# added to an existing cloud-backup scope; their authorized source must be kept.
MODELS.update({'observation_index':(ObservationSnapshot,{'comments_json'}),
               'offline_comment_cursors':(OfflineCommentCursor,set())})

def snapshot(store,deadline,max_bytes):
    from . import main
    from .models import LocalMediaAsset
    from .video_backup import BackupError
    data={'format':'business-portable-v1','tables':{},'role_grants_restored':False,'external_deliveries_replayed':False,
        'restore_requires_admin_review':True,'excluded':['credentials','token_hashes','role_grants','CRM_config','raw_CRM_payloads','external_Feishu_data','account_sessions']}
    evidence=[];pending=[];budget=0;new_bytes=0;reused=0;root=Path(main.cfg.db_path).absolute().parent/'business-proofs';uploads=Path(main.UPLOAD_DIR).resolve()
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN')
        for name,(model,exclude) in MODELS.items():
            rows=s.exec(select(model).limit(5001)).all()
            if len(rows)>5000:raise BackupError('business_metadata_limit')
            data['tables'][name]=[row_dict(r,exclude) for r in rows]
        for p in s.exec(select(DistributionProof).limit(1001)).all():
            if len(evidence)>=1000:raise BackupError('business_evidence_limit')
            path=Path(p.file_path)
            if path.parent!=root or path.suffix.lower() not in {'.png','.jpg','.webp'}:raise BackupError('business_evidence_path_invalid')
            entry={'source':'proofs','relative':path.name,'size':p.size,'mtime_ns':path.stat().st_mtime_ns}
            budget+=p.size
            if budget>max_bytes:raise BackupError('snapshot_size_limit')
            entry['sha256']=p.sha256;evidence.append(entry);pending.append((root,entry,'business_evidence_corrupt'))
        ids=set()
        for v in data['tables']['versions']:ids.update(json.loads(v['asset_ids_json']))
        asset_index=[]
        for aid in sorted(ids):
            a=s.get(LocalMediaAsset,aid)
            if not a:raise BackupError('business_asset_missing')
            entries=[]
            for m in json.loads(a.manifest_json):
                path=Path(m['path'])
                if not path.resolve().is_relative_to(uploads) or path.suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.mp4','.mov','.m4v','.webm','.mkv','.avi'}:raise BackupError('business_asset_path_invalid')
                if len(evidence)>=1000:raise BackupError('business_evidence_limit')
                budget+=m['size']
                if budget>max_bytes:raise BackupError('snapshot_size_limit')
                entry={'source':'business-assets','relative':path.resolve().relative_to(uploads).as_posix(),'size':m['size'],'mtime_ns':path.stat().st_mtime_ns}
                entry['sha256']=m['sha256'];evidence.append(entry);pending.append((uploads,entry,'business_asset_corrupt'))
                entries.append({'sha256':m['sha256'],'size':m['size'],'relative':entry['relative']})
            asset_index.append({'id':a.id,'label':a.label,'media_type':a.media_type,'is_demo':a.is_demo,'objects':entries})
        data['asset_index']=asset_index
    # Release the coherent SQLite read view BEFORE remote IO and durable
    # cloud receipt commits; rollback-journal databases otherwise self-deadlock.
    for source,entry,error in pending:
        h,old=store.put_source(source,entry,deadline)
        reused+=int(old);new_bytes+=0 if old else entry['size']
        if h!=entry['sha256']:raise BackupError(error)
    data['credential_reissue_required']=True
    data['account_mapping_review_required']=True
    return data,evidence,{'bytes':budget,'new_object_bytes':new_bytes,'reused_files':reused}


def validate(data,evidence):
    from .video_backup import BackupError,relpath
    if not isinstance(data,dict) or data.get('format')!='business-portable-v1' or not isinstance(data.get('tables'),dict):raise BackupError('business_manifest_invalid')
    if not LEGACY_TABLES<=set(data['tables'])<=set(MODELS) or not isinstance(evidence,list) or len(evidence)>1000:raise BackupError('business_manifest_invalid')
    for e in evidence:
        if e.get('source') not in {'proofs','business-assets'} or type(e.get('size')) is not int or not 0<e['size']<=2**40:raise BackupError('business_manifest_invalid')
        relpath(e['relative'])
        if Path(e['relative']).suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.mp4','.mov','.m4v','.webm','.mkv','.avi'}:raise BackupError('business_manifest_invalid')
    # Counts/IDs/hash relationships survive, permissions and external effects do not.
    targets={r['id'] for r in data['tables']['targets']};tasks={r['id'] for r in data['tables']['tasks']}
    for p in data['tables']['proofs']:
        if p['target_id'] not in targets or p['task_id'] not in tasks or not any(e['sha256']==p['sha256'] and e['source']=='proofs' for e in evidence):raise BackupError('business_relationship_invalid')
