"""REQ-014/015/030: versioned video + portable catalog/rules, not mirroring.

Local directory/NAS and opt-in Feishu Drive providers. No platform fetch,
credential export, deletes or automatic restore/catch-up. Cloud upload of
business data is separately authorized, not implied by a connection check.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime
try:
    import fcntl
except ImportError:  # Do not prevent the existing Hub from starting on Windows.
    fcntl=None
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
from typing import Protocol
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool
from sqlmodel import select

from .hub_adapter import get_session
from .hub_adapter import VideoBackupConfig, VideoBackupOperation, LocalMediaAsset, ContentRecord, ShareDownloadRecord, CommentRule, DmAutoReplyRule, RiskAdminAudit
from .hub_adapter import utc_iso

router=APIRouter()
FORMAT='creatorhub-video-backup-v1'
VIDEO_EXT={'.mp4','.mov','.m4v','.webm','.mkv'}
MAX_FILES=5000
MAX_ENTRIES=50000
MAX_MANIFEST=8*1024*1024
MIN_FREE=512*1024*1024
DEADLINE=600
ID_RE=r'vbak_[a-f0-9]{32}'


class BackupError(Exception):
    pass


def packed(data):
    return json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


def sha(data):return hashlib.sha256(data).hexdigest()


def directory(raw):
    p=Path(raw).expanduser()
    if not p.is_absolute() or not p.is_dir():raise BackupError('directory_unavailable')
    for part in [p,*p.parents]:
        if part.is_symlink():raise BackupError('symlink_rejected')
    if p==Path(p.anchor):raise BackupError('directory_too_broad')
    return p.resolve(strict=True)


def relpath(value):
    p=PurePosixPath(value)
    if p.is_absolute() or not p.parts or any(x in {'.','..'} for x in p.parts) or '\\' in value or '\x00' in value:
        raise BackupError('unsafe_relative_path')
    return p


@contextmanager
def open_source(root,relative):
    """Reject symlink traversal and non-regular files, including mid-copy swaps."""
    if fcntl is None or not hasattr(os,'O_NOFOLLOW'):raise BackupError('local_provider_requires_posix')
    p=relpath(relative);fds=[]
    try:
        fd=os.open(directory(root),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(fd)
        for name in p.parts[:-1]:
            fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(fd)
        f=os.open(p.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        with os.fdopen(f,'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise BackupError('not_regular_file')
            yield stream
    finally:
        for fd in reversed(fds):os.close(fd)


def stream_hash(stream,deadline=None):
    h=hashlib.sha256();size=0
    while chunk:=stream.read(1024*1024):
        if deadline and time.monotonic()>deadline:raise BackupError('io_deadline')
        h.update(chunk);size+=len(chunk)
    return h.hexdigest(),size


def source_roots():
    from .hub_adapter import main_module
    main = main_module()
    from .hub_adapter import get_setting
    roots={'downloads':Path(get_setting('download_dir',main.cfg.engine.media_dir) or main.cfg.engine.media_dir).expanduser().absolute(),
           'uploads':Path(main.UPLOAD_DIR).absolute()}
    private=[Path(main.cfg.engine.profiles_dir).absolute().resolve(),Path.home()/'.config',Path.home()/'.ssh']
    for name,root in roots.items():
        roots[name]=directory(root) if root.exists() else root.resolve()
        if any(roots[name]==p or roots[name].is_relative_to(p) for p in private):raise BackupError('private_source_rejected')
    # Deliberately not a recursive backup of data/, DB, profile or .config.
    return roots


def inventory():
    roots=source_roots();files=[];entries=0;skipped=0;unavailable=[];deadline=time.monotonic()+30
    for alias,root in roots.items():
        if not root.exists():unavailable.append(alias);continue
        directory(root)
        def onerror(_):raise BackupError('source_unreadable')
        for base,dirs,names in os.walk(root,followlinks=False,onerror=onerror):
            if time.monotonic()>deadline:raise BackupError('inventory_deadline')
            entries+=len(dirs)+len(names)
            if entries>MAX_ENTRIES:raise BackupError('inventory_limit')
            dirs[:]=[d for d in dirs if not d.startswith('.') and d not in {'profiles','backups','creatorhub-video-backups'} and not (Path(base)/d).is_symlink()]
            for name in sorted(names):
                if name.startswith('.'):continue
                path=Path(base)/name
                if path.suffix.lower() not in VIDEO_EXT:continue
                st=path.lstat()
                if not stat.S_ISREG(st.st_mode) or not st.st_size:skipped+=1;continue
                files.append({'source':alias,'relative':str(path.relative_to(root)),
                              'size':st.st_size,'mtime_ns':st.st_mtime_ns})
                if len(files)>MAX_FILES:raise BackupError('inventory_limit')
    # Overlapping configured roots must not duplicate physical files.
    unique={str(roots[f['source']]/f['relative']):f for f in files}
    return roots,list(unique.values()),{'video_files':len(unique),'bytes':sum(f['size'] for f in unique.values()),
        'skipped_files':skipped,'complete':not unavailable,'unavailable_sources':unavailable,'platform_fetch_triggered':False}


def protected_target(base):
    """A destination cannot sit inside a source tree (recursive backup trap)."""
    from .hub_adapter import main_module
    main = main_module()
    forbidden=[*source_roots().values(),Path(main.cfg.engine.profiles_dir).absolute(),Path.home()/'.config']
    for root in forbidden:
        if base==root or base.is_relative_to(root) or root.is_relative_to(base):
            raise BackupError('destination_overlaps_source_or_private_data')


class BackupStore(Protocol):
    """Provider v1 seam. An implementation must preserve immutable key semantics."""
    def lock(self): ...
    def available_bytes(self)->int|None: ...
    def same_filesystem(self,roots:dict)->bool|None: ...
    def put_source(self,root:Path,entry:dict,deadline:float)->tuple[str,bool]: ...
    def put_manifest(self,snapshot_id:str,data:bytes)->None: ...
    def read_manifest(self,snapshot_id:str)->bytes: ...
    def verify_object(self,digest:str,size:int,deadline:float)->None: ...
    def restore_object(self,digest:str,size:int,dest:Path,deadline:float)->None: ...


class LocalDirectoryStore:
    def __init__(self,root,store_id='',*,initialize=False):
        if fcntl is None or not hasattr(os,'O_NOFOLLOW'):raise BackupError('local_provider_requires_posix')
        self.base=directory(root);protected_target(self.base)
        self.path=self.base/'creatorhub-video-backups'
        if initialize:
            if not self.path.exists():
                self.path.mkdir(mode=0o700)
                self._write_new(self.path/'store.json',packed({'format':FORMAT,'store_id':uuid.uuid4().hex}))
            if not (self.path/'store.json').is_file():raise BackupError('destination_not_owned_by_hub')
        directory(self.path)
        with open_source(self.path,'store.json') as f:raw=f.read(4097)
        marker=json.loads(raw)
        if len(raw)>4096 or marker.get('format')!=FORMAT or not re.fullmatch('[a-f0-9]{32}',str(marker.get('store_id',''))):
            raise BackupError('invalid_store_marker')
        self.store_id=marker['store_id']
        if store_id and self.store_id!=store_id:raise BackupError('destination_identity_changed')
        for d in ['objects','snapshots','tmp']:
            p=self.path/d
            if p.exists():directory(p)
            elif initialize:p.mkdir(mode=0o700)
            else:raise BackupError('destination_layout_missing')

    def available_bytes(self):return shutil.disk_usage(self.path).free

    def same_filesystem(self,roots):
        return all(p.stat().st_dev==self.path.stat().st_dev for p in roots.values() if p.exists())

    @staticmethod
    def _write_new(path,data):
        with path.open('xb') as f:
            os.chmod(path,0o600);f.write(data);f.flush();os.fsync(f.fileno())

    @contextmanager
    def lock(self):
        fd=os.open(self.path/'io.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise BackupError('storage_busy') from None
            yield
        finally:os.close(fd)

    def object_path(self,digest):
        if not re.fullmatch('[a-f0-9]{64}',digest):raise BackupError('invalid_object_digest')
        return self.path/'objects'/digest

    def verify_object(self,digest,size,deadline):
        with open_source(self.path,'objects/'+digest) as f:actual,n=stream_hash(f,deadline)
        if actual!=digest or n!=size:raise BackupError('object_corrupt')

    def put_source(self,root,entry,deadline):
        temp=self.path/'tmp'/uuid.uuid4().hex
        try:
            with open_source(root,entry['relative']) as src:
                before=os.fstat(src.fileno())
                if (before.st_size,before.st_mtime_ns)!=(entry['size'],entry['mtime_ns']):raise BackupError('source_changed')
                h=hashlib.sha256();size=0
                with temp.open('xb') as dst:
                    os.chmod(temp,0o600)
                    while chunk:=src.read(1024*1024):
                        if time.monotonic()>deadline:raise BackupError('io_deadline')
                        dst.write(chunk);h.update(chunk);size+=len(chunk)
                    dst.flush();os.fsync(dst.fileno())
                after=os.fstat(src.fileno())
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or size!=before.st_size:
                    raise BackupError('source_changed')
            digest=h.hexdigest();dest=self.object_path(digest);reused=dest.exists()
            if reused:self.verify_object(digest,size,deadline)
            else:
                # Link publishes only a fully written file; never overwrite an object.
                os.link(temp,dest);self.verify_object(digest,size,deadline)
            return digest,reused
        finally:
            # Only this operation's random temporary file is removed, never source/history.
            if temp.exists():temp.unlink()

    def put_manifest(self,snapshot_id,data):
        if not re.fullmatch(ID_RE,snapshot_id) or len(data)>MAX_MANIFEST:raise BackupError('manifest_invalid')
        self._write_new(self.path/'snapshots'/(snapshot_id+'.json'),data)

    def read_manifest(self,snapshot_id):
        if not re.fullmatch(ID_RE,snapshot_id):raise BackupError('manifest_invalid')
        with open_source(self.path,'snapshots/'+snapshot_id+'.json') as f:data=f.read(MAX_MANIFEST+1)
        if len(data)>MAX_MANIFEST:raise BackupError('manifest_invalid')
        return data

    def restore_object(self,digest,size,dest,deadline):
        with open_source(self.path,'objects/'+digest) as source,dest.open('xb') as output:
            os.chmod(dest,0o600);h=hashlib.sha256();n=0
            while chunk:=source.read(1024*1024):
                if time.monotonic()>deadline:raise BackupError('io_deadline')
                output.write(chunk);h.update(chunk);n+=len(chunk)
            output.flush();os.fsync(output.fileno())
        if h.hexdigest()!=digest or n!=size:raise BackupError('object_corrupt')


def make_store(provider,root,store_id='',**kw)->BackupStore:
    if provider=='feishu_drive':
        from .feishu_drive import FeishuDriveStore
        return FeishuDriveStore(root,store_id,**kw)
    if provider!='local_directory':raise BackupError('provider_not_implemented')
    return LocalDirectoryStore(root,store_id,**kw)


def portable_metadata(roots,files):
    """Allowlist export: no DB copy, cookies, profiles, tokens or presigned URLs."""
    from .hub_adapter import main_module
    main = main_module()
    by_path={str((roots[f['source']]/f['relative']).resolve()):f['sha256'] for f in files}
    assets=[];contents=[];shares=[];comment_rules=[];dm_rules=[]
    with get_session() as s:
        for a in s.exec(select(LocalMediaAsset).where(LocalMediaAsset.media_type=='video')).all():
            refs=[by_path.get(str(Path(m['path']).resolve())) for m in json.loads(a.manifest_json)]
            if refs and all(refs):assets.append({'id':a.id,'label':a.label,'is_demo':a.is_demo,'sha256':refs})
        for c in s.exec(select(ContentRecord).where(ContentRecord.download_status=='done')).all():
            if not c.local_path:continue
            local=Path(c.local_path).resolve()
            refs=[digest for path,digest in by_path.items() if Path(path)==local or Path(path).is_relative_to(local)]
            if refs:contents.append({'id':c.id,'platform':c.platform,'item_id':c.aweme_id,'title':c.desc,'sha256':refs})
        for c in s.exec(select(ShareDownloadRecord).where(ShareDownloadRecord.status=='done')).all():
            paths=[p.get('path','') if isinstance(p,dict) else p for p in json.loads(c.files_json)]
            refs=[by_path.get(str(Path(p).resolve())) for p in paths if isinstance(p,str) and p]
            refs=[x for x in refs if x]
            if refs:shares.append({'id':c.id,'platform':c.platform,'item_id':c.item_id,'title':c.title,'account_id':c.account_id,'sha256':refs})
        for model,output,fields in [
            (CommentRule,comment_rules,['id','platform','name','mode','account_id','target_kind','keyword','sec_uid','aweme_id','templates','use_ai','reply_filter','skip_keywords','daily_cap','min_gap_seconds','max_per_run','interval_seconds','require_review','enabled']),
            (DmAutoReplyRule,dm_rules,['id','platform','account_id','name','enabled','match_mode','keywords','exclude_keywords','reply_templates','review_before_send','min_delay_seconds','max_delay_seconds','cooldown_seconds','max_message_age_seconds'])]:
            for rule in s.exec(select(model).limit(5001)).all():
                if len(output)>=5000:raise BackupError('metadata_limit')
                output.append({k:getattr(rule,k) for k in fields})
    fields=['quiet_hours_enabled','active_hours_start','active_hours_end','active_accounts','work_health_enabled']
    from .operations_workflows import SIGNALS,NEGATIVE
    return {'assets':assets,'download_records':contents,'share_downloads':shares}, {'format':'nonsecret-policy-v1',
        'engine':{k:getattr(main.cfg.engine,k) for k in fields},
        'offline_intent_rules':{'signals':SIGNALS,'negative':NEGATIVE},'comment_rules':comment_rules,'dm_reply_rules':dm_rules,
        'restore_requires_admin_review':True,'automatically_reenabled':False,
        'backup':{'retention':'keep_all_no_delete','automatic_schedule_enabled':False,'restore':'new_isolated_directory_only'},
        'excluded':['credentials','account_database','profiles','customer_contacts','external_feishu_data'],
        'coverage_note':'Only this explicit policy allowlist; not a complete business DB/Feishu export.'}


def load_manifest(store,snapshot):
    raw=store.read_manifest(snapshot.id)
    if sha(raw)!=snapshot.manifest_sha256:raise BackupError('manifest_corrupt')
    data=json.loads(raw)
    if data.get('format')!=FORMAT or data.get('id')!=snapshot.id or not isinstance(data.get('files'),list) or len(data['files'])>MAX_FILES:
        raise BackupError('manifest_invalid')
    for f in data['files']:
        if f.get('source') not in {'downloads','uploads'} or not isinstance(f.get('relative'),str):raise BackupError('manifest_invalid')
        relpath(f['relative'])
        if type(f.get('size')) is not int or f['size']<=0 or Path(f['relative']).suffix.lower() not in VIDEO_EXT:raise BackupError('manifest_invalid')
    if 'business' in data:
        from .business_backup import validate
        validate(data['business'],data.get('business_evidence',[]))
    return data


def run_operation(oid):
    start=time.monotonic();store=None
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE');row=s.get(VideoBackupOperation,oid)
        if not row or row.status!='pending':return
        row.status='running';s.add(row);s.commit();s.refresh(row)
        snapshot=s.get(VideoBackupOperation,row.snapshot_id) if row.snapshot_id else None
    try:
        store=make_store(row.provider,row.root,row.store_id);deadline=start+DEADLINE
        with store.lock():
            if row.kind=='snapshot':
                if row.provider=='feishu_drive' and not row.cloud_upload_authorized:raise BackupError('cloud_business_upload_not_authorized')
                roots,files,summary=inventory()
                if not summary['complete']:raise BackupError('source_storage_unavailable')
                if not files:raise BackupError('no_local_videos')
                if summary['bytes']>row.max_bytes:raise BackupError('snapshot_size_limit')
                available=store.available_bytes()
                if available is not None and available<summary['bytes']+MIN_FREE:raise BackupError('insufficient_space')
                reused=new_bytes=0
                for entry in files:
                    digest,old=store.put_source(roots[entry['source']],entry,deadline)
                    entry['sha256']=digest;reused+=int(old)
                    if not old:new_bytes+=entry['size']
                catalog,rules=portable_metadata(roots,files)
                from .business_backup import snapshot as business_snapshot
                business,evidence,business_stats=business_snapshot(store,deadline,row.max_bytes-summary['bytes'])
                data={'format':FORMAT,'id':row.id,'created_at':utc_iso(row.created_at),'files':files,
                      'catalog':catalog,'rules':rules,'business':business,'business_evidence':evidence,'retention':'keep_all_no_delete'}
                raw=packed(data);store.put_manifest(row.id,raw);row.manifest_sha256=sha(raw)
                result={**summary,'video_bytes':summary['bytes'],'bytes':summary['bytes']+business_stats['bytes'],'business_evidence_bytes':business_stats['bytes'],'reused_files':reused+business_stats['reused_files'],'new_object_bytes':new_bytes+business_stats['new_object_bytes'],
                        'business_evidence_files':len(evidence),'business_package_included':True,'same_filesystem':store.same_filesystem(roots),
                        'independent_backup_verified':False,'objects_verified':True}
            else:
                if not snapshot or snapshot.status!='done' or snapshot.kind!='snapshot':raise BackupError('snapshot_unavailable')
                data=load_manifest(store,snapshot)
                for f in data['files']+data.get('business_evidence',[]):store.verify_object(f['sha256'],f['size'],deadline)
                result={'video_files':len(data['files']),'bytes':sum(f['size'] for f in data['files']+data.get('business_evidence',[])),'video_bytes':sum(f['size'] for f in data['files']),'business_evidence_bytes':sum(f['size'] for f in data.get('business_evidence',[])),'objects_verified':True}
                if row.kind=='restore':
                    if row.restore_parent:parent=directory(row.restore_parent)
                    else:
                        from .hub_adapter import main_module
                        main = main_module()
                        parent=Path(main.cfg.db_path).absolute().parent/'video-restore-drills'
                        parent.mkdir(mode=0o700,exist_ok=True);parent=directory(parent)
                    protected_target(parent)
                    dest=parent/('creatorhub-restore-'+row.id);dest.mkdir(mode=0o700)
                    for f in data['files']+data.get('business_evidence',[]):
                        target=dest/f['source']/relpath(f['relative']);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                        store.restore_object(f['sha256'],f['size'],target,deadline)
                    LocalDirectoryStore._write_new(dest/'catalog.json',packed(data['catalog']))
                    LocalDirectoryStore._write_new(dest/'rules.json',packed(data['rules']))
                    if 'business' in data:LocalDirectoryStore._write_new(dest/'business.json',packed(data['business']))
                    LocalDirectoryStore._write_new(dest/'manifest.json',packed(data))
                    result.update(restore_path=str(dest),isolated_restore=True,live_data_overwritten=False,
                                  rules_and_catalog_restored=True,business_package_restored='business' in data,credentials_reissued=False,role_grants_applied=False,external_events_replayed=False,rpo_target_evaluated=False,rto_target_evaluated=False)
        result['elapsed_seconds']=round(time.monotonic()-start,3)
        if row.kind=='restore':
            from .delivery_profile import drill_targets
            drill_targets(result,snapshot.created_at,row.created_at,result['elapsed_seconds'])
        with get_session() as s:
            live=s.get(VideoBackupOperation,oid);live.status='done';live.finished_at=datetime.utcnow()
            live.result_json=json.dumps(result);live.manifest_sha256=row.manifest_sha256;s.add(live);s.commit()
    except Exception as exc:
        with get_session() as s:
            live=s.get(VideoBackupOperation,oid);live.status='failed';live.finished_at=datetime.utcnow()
            live.error_code=str(exc) if isinstance(exc,BackupError) else 'local_io_failed'
            live.result_json=json.dumps({'elapsed_seconds':round(time.monotonic()-start,3),'partial_files_may_exist':True,
                                       'source_deleted':False,'platform_write_triggered':False})
            s.add(live);s.commit()

    finally:
        if store is not None and callable(getattr(store,"close",None)):store.close()


def recover():
    with get_session() as s:
        rows=s.exec(select(VideoBackupOperation).where(VideoBackupOperation.status.in_(['pending','running']))).all()
        for row in rows:
            row.status='interrupted';row.error_code='service_restarted_no_automatic_replay';row.finished_at=datetime.utcnow();s.add(row)
        s.commit();count=len(rows)
    from .feishu_drive import recover as cloud_recover
    return count+cloud_recover()


def public(row,*,detail=False):
    data={k:getattr(row,k) for k in ['id','kind','snapshot_id','status','error_code','manifest_sha256']}
    result=json.loads(row.result_json)
    if not detail:result.pop('restore_path',None)
    return {**data,'created_at':utc_iso(row.created_at),'finished_at':utc_iso(row.finished_at),'result':result}


class ConfigIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    provider:str=Field(pattern='^(unconfigured|local_directory|feishu_drive)$')
    root:str=Field(default='',max_length=4096)
    max_bytes:StrictInt=Field(default=10*1024**3,gt=0,le=100*1024**3)
    cloud_upload_authorized:StrictBool=False


class OperationIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    snapshot_id:str=Field(default='',pattern='^('+ID_RE+')?$')
    restore_parent:str=Field(default='',max_length=4096)


@router.get('/api/video-backups')
def list_operations():
    with get_session() as s:
        return [public(r) for r in s.exec(select(VideoBackupOperation).order_by(VideoBackupOperation.created_at.desc()).limit(100)).all()]


@router.get('/api/video-backups/status')
def backup_status():
    with get_session() as s:
        cfg=s.get(VideoBackupConfig,1) or VideoBackupConfig()
        last=s.exec(select(VideoBackupOperation).where(VideoBackupOperation.kind=='snapshot',VideoBackupOperation.status=='done').order_by(VideoBackupOperation.created_at.desc())).first()
        state={'configured':cfg.provider!='unconfigured','provider':cfg.provider,'destination_available':False,
            'provider_interface_version':'1.0','implemented_providers':['local_directory','feishu_drive'],'root':cfg.root,
            'cloud_upload_authorized':cfg.cloud_upload_authorized,'external_io':cfg.provider=='feishu_drive',
            'max_bytes':cfg.max_bytes,'retention':'keep_all_no_delete','automatic_schedule_enabled':False,
            'independent_backup_verified':False,'last_success':public(last) if last else None,
            'coverage':'videos + linked catalog + nonsecret policies + portable business/evidence package; excludes account DB/credentials/external Feishu data',
            'cloud_file_limit_verified':False,'cloud_quota_available_bytes':None,
            'requirements':['REQ-014','REQ-015','REQ-030'],'ac_014_customer_acceptance':'pending_destination_and_rpo_rto'}
        try:
            _,_,state['inventory']=inventory()
            if state['configured']:
                store=make_store(cfg.provider,cfg.root,cfg.store_id);state['destination_available']=True
                if callable(getattr(store,'close',None)):store.close()
        except (BackupError,OSError,ValueError) as e:state['error_code']=str(e) if isinstance(e,BackupError) else 'local_io_failed'
        return state


def close_store(store):
    if store is not None and callable(getattr(store,'close',None)):store.close()


@router.post('/api/video-backups/config')
def configure(body:ConfigIn,request:Request):
    from .hub_adapter import _require_risk_admin
    from .hub_adapter import submit_once, replay_if_exists
    actor=_require_risk_admin(request)
    if not request.headers.get('Idempotency-Key'):raise HTTPException(422,'需要稳定的 Idempotency-Key')
    if body.provider!='feishu_drive' and body.cloud_upload_authorized:raise HTTPException(422,'云端上传授权只适用于飞书云盘')
    with get_session() as s:
        old=replay_if_exists(s,request=request,scope='video-backup:config',body=body)
        if old:return old
        if s.exec(select(VideoBackupOperation).where(VideoBackupOperation.status.in_(['pending','running']))).first():raise HTTPException(409,'备份操作执行中，暂不更改目标')
    # Never keep SQLite's writer lock during cloud IO or nested journal commits.
    store=None
    try:
        if body.provider=='unconfigured':
            if body.root:raise HTTPException(422,'未配置状态不接受路径')
            root=store_id=''
        else:
            store=make_store(body.provider,body.root,initialize=True);root=str(store.base);store_id=store.store_id
    except (BackupError,OSError,ValueError) as e:raise HTTPException(422,str(e) if isinstance(e,BackupError) else '目标不可用') from None
    finally:close_store(store)
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        def create():
            if s.exec(select(VideoBackupOperation).where(VideoBackupOperation.status.in_(['pending','running']))).first():raise HTTPException(409,'备份操作并发开始，目标未修改；请核对后再配置')
            cfg=s.get(VideoBackupConfig,1) or VideoBackupConfig()
            cfg.provider=body.provider;cfg.root=root;cfg.store_id=store_id;cfg.max_bytes=body.max_bytes;cfg.cloud_upload_authorized=body.cloud_upload_authorized;cfg.updated_at=datetime.utcnow();s.add(cfg)
            s.add(RiskAdminAudit(action='video_backup_target_configured',actor=actor,detail=json.dumps({'provider':body.provider,'external_provider_enabled':body.provider=='feishu_drive','cloud_upload_authorized':body.cloud_upload_authorized})))
            return {'configured':body.provider!='unconfigured','provider':body.provider,'independent_backup_verified':False,'cloud_upload_authorized':body.cloud_upload_authorized}
        data,_=submit_once(s,request=request,scope='video-backup:config',body=body,create=create)
        return data


@router.get('/api/video-backups/{oid}')
def operation_status(oid:str):
    with get_session() as s:
        row=s.get(VideoBackupOperation,oid)
        if not row:raise HTTPException(404,'备份操作不存在')
        return public(row,detail=True)


@router.post('/api/video-backups/{kind}')
def submit_operation(kind:str,body:OperationIn,request:Request,background:BackgroundTasks):
    from .hub_adapter import _require_risk_admin
    actor=_require_risk_admin(request)
    if kind not in {'snapshot','verify','restore'}:raise HTTPException(422,'不支持的备份动作')
    key=request.headers.get('Idempotency-Key','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{15,127}',key):raise HTTPException(422,'需要16–128位稳定提交编号')
    if bool(body.snapshot_id)!=(kind!='snapshot') or (body.restore_parent and kind!='restore'):raise HTTPException(422,'操作与快照绑定不匹配')
    identity=sha((kind+'\0'+key).encode());payload=sha(packed(body.model_dump()))
    def previous(s):
        old=s.exec(select(VideoBackupOperation).where(VideoBackupOperation.request_hash==identity)).first()
        if old:
            if old.payload_hash!=payload:raise HTTPException(409,'编号已用于不同内容')
            return {**public(old),'replayed':True}
    with get_session() as s:
        old=previous(s)
        if old:return old
        if s.exec(select(VideoBackupOperation).where(VideoBackupOperation.status.in_(['pending','running']))).first():raise HTTPException(409,'已有备份IO操作在执行')
        if kind=='snapshot':
            source=s.get(VideoBackupConfig,1)
            if not source or source.provider=='unconfigured':raise HTTPException(409,'备份目标尚未配置，不选择默认云盘或服务器')
            if source.provider=='feishu_drive' and not source.cloud_upload_authorized:raise HTTPException(409,'云盘已连接，但未授权上传业务备份；不会上传视频、私有截图或业务包')
        else:
            source=s.get(VideoBackupOperation,body.snapshot_id)
            if not source or source.kind!='snapshot' or source.status!='done':raise HTTPException(409,'快照未完成或不存在')
        binding={k:getattr(source,k) for k in ('provider','root','store_id','max_bytes','cloud_upload_authorized')}
    store=None
    try:
        store=make_store(binding['provider'],binding['root'],binding['store_id'])
        if body.restore_parent:protected_target(directory(body.restore_parent))
    except (BackupError,OSError,ValueError) as e:raise HTTPException(409,str(e) if isinstance(e,BackupError) else '目标不可用') from None
    finally:close_store(store)
    with get_session() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        old=previous(s)
        if old:return old
        if s.exec(select(VideoBackupOperation).where(VideoBackupOperation.status.in_(['pending','running']))).first():raise HTTPException(409,'已有备份IO操作在执行')
        live=s.get(VideoBackupConfig,1) if kind=='snapshot' else s.get(VideoBackupOperation,body.snapshot_id)
        if not live or {k:getattr(live,k) for k in binding}!=binding or (kind!='snapshot' and live.status!='done'):raise HTTPException(409,'目标或快照并发变化，未创建操作')
        row=VideoBackupOperation(id='vbak_'+uuid.uuid4().hex,request_hash=identity,payload_hash=payload,kind=kind,
            snapshot_id=body.snapshot_id,**binding,restore_parent=body.restore_parent)
        s.add(row);s.add(RiskAdminAudit(action='video_backup_'+kind,actor=actor,detail=json.dumps({'operation_id':row.id,'snapshot_id':row.snapshot_id})));s.commit();s.refresh(row)
        background.add_task(run_operation,row.id)
        return public(row)
