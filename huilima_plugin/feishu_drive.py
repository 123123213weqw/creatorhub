"""Opt-in Feishu Drive store, never a browser-cookie bridge or whole-drive sync.

First-party contracts: drive/v1/files upload_all / upload_prepare / upload_part /
 upload_finish / download / create_folder / list, verified against Lark SDK.
No DELETE/MOVE/permission API; no automatic POST retries after ambiguity.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime
import hashlib,io,json,logging,math,os,re,stat,threading,time,uuid,zlib
from pathlib import Path
from urllib.parse import urlsplit,urljoin
import httpx
from sqlmodel import select
from .hub_adapter import get_session
from .cloud_storage_models import CloudStorageWrite
from .video_backup import BackupError,FORMAT,MAX_MANIFEST,ID_RE,open_source,stream_hash,packed

API='https://open.feishu.cn/open-apis'
SMALL_LIMIT=20*1024*1024
MULTIPART_THRESHOLD=8*1024*1024  # Bounded single-upload replay surface; parts use server block size.
MAX_FILE=10*1024**3
MAX_LIST=6000
TOKEN_RE=r'[A-Za-z0-9_-]{5,160}'
TOKEN_CACHE={};TOKEN_LOCK=threading.Lock()

class _SignedURLFilter(logging.Filter):
    def filter(self,record):
        message=record.getMessage()
        record.msg=re.sub(r'(https://[^\s?]+)\?[^\s]+',r'\1?[query-redacted]',message);record.args=()
        return True
if not any(isinstance(f,_SignedURLFilter) for f in logging.getLogger('httpx').filters):logging.getLogger('httpx').addFilter(_SignedURLFilter())



def valid_token(value):
    if not isinstance(value,str) or not re.fullmatch(TOKEN_RE,value):raise BackupError('cloud_token_invalid')
    return value


def root_parts(raw):
    if not isinstance(raw,str):raise BackupError('cloud_folder_url_invalid')
    u=urlsplit(raw)
    try:port=u.port
    except ValueError:raise BackupError('cloud_folder_url_invalid') from None
    if (u.scheme!='https' or not u.hostname or not u.hostname.endswith('.feishu.cn') or u.username or u.password
        or u.query or u.fragment or port not in (None,443) or not re.fullmatch(r'[a-z0-9-]+\.feishu\.cn',u.hostname)
        or not re.fullmatch('/drive/folder/'+TOKEN_RE+'/?',u.path)):
        raise BackupError('cloud_folder_url_invalid')
    return u.hostname,valid_token(u.path.rstrip('/').split('/')[-1])


def credential_path():
    raw=os.environ.get('CREATORHUB_FEISHU_CREDENTIALS','')
    return Path(raw) if raw else Path.home()/'.config/creatorhub-feishu-drive/credentials.json'


def load_credentials(path=None):
    p=Path(path) if path else credential_path()
    if not p.is_absolute() or p.is_symlink():raise BackupError('cloud_credentials_private_file_required')
    try:
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_mode&0o077 or st.st_size>4096:raise BackupError('cloud_credentials_private_file_required')
            d=json.loads(f.read(4097))
        if set(d)!={'app_id','app_secret'} or not re.fullmatch(r'cli_[a-zA-Z0-9]{5,80}',d['app_id']) or not isinstance(d['app_secret'],str) or not 8<=len(d['app_secret'])<=512:raise ValueError()
        return d
    except (OSError,ValueError,KeyError,TypeError):raise BackupError('cloud_credentials_unavailable') from None


def token(http,*,path=None,invalidate=False):
    c=load_credentials(path);key=c['app_id']+':'+hashlib.sha256(c['app_secret'].encode()).hexdigest()
    with TOKEN_LOCK:
        cached=TOKEN_CACHE.get(key)
        if cached and not invalidate and time.monotonic()<cached[1]:return cached[0],c['app_id']
        try:
            r=http.post(API+'/auth/v3/tenant_access_token/internal',json=c)
            v=r.json()
            if r.status_code!=200 or v.get('code')!=0:raise BackupError('cloud_app_auth_rejected')
            t=v['tenant_access_token'];expire=v['expire']
            if not isinstance(t,str) or not t or type(expire) is not int or expire<=60:raise ValueError()
            TOKEN_CACHE[key]=(t,time.monotonic()+min(expire-60,7200));return t,c['app_id']
        except (httpx.HTTPError,ValueError,KeyError,TypeError):raise BackupError('cloud_app_auth_unavailable') from None


class FeishuAPI:
    def __init__(self,http=None,credentials=None):
        self.http=http or httpx.Client(timeout=httpx.Timeout(40,connect=10),follow_redirects=False,trust_env=False)
        self.credentials=credentials
        self.app_id=load_credentials(credentials)['app_id']

    def close(self):self.http.close()

    def request(self,method,path,**kw):
        if not path.startswith('/drive/v1/') or '..' in path or '?' in path:raise BackupError('cloud_route_rejected')
        for attempt in range(2):
            try:
                access,_=token(self.http,path=self.credentials)
                r=self.http.request(method,API+path,headers={'Authorization':'Bearer '+access},**kw)
                if method=='GET' and r.status_code in {429,502,503,504} and attempt==0:time.sleep(.3);continue
                if r.status_code>=500:raise BackupError('cloud_submission_unknown' if method!='GET' else 'cloud_read_unavailable')
                if r.status_code in {301,302,303,307,308}:raise BackupError('cloud_unexpected_redirect')
                data=r.json();code=data.get('code')
                if code in {99991663,99991664,99991668}:
                    if method=='GET' and attempt==0:token(self.http,path=self.credentials,invalidate=True);continue
                    raise BackupError('cloud_access_token_rejected')
                if code in {99991672,99991679} or r.status_code in {401,403}:raise BackupError('cloud_scope_or_resource_permission_missing')
                if r.status_code!=200 or code!=0:
                    known={1061043:'cloud_file_size_limit',1061101:'cloud_tenant_quota_exceeded',1061061:'cloud_user_quota_exceeded',1061045:'cloud_rate_limited',1062507:'cloud_folder_node_limit',1062008:'cloud_part_checksum_rejected',1062009:'cloud_part_size_rejected'}
                    raise BackupError(known.get(code,'cloud_request_rejected_'+str(code) if type(code) is int else 'cloud_response_invalid'))
                value=data.get('data',{})
                # upload_part has no response payload; Feishu also returns null
                # for this unit response. code=0 is authoritative for the part.
                if path=='/drive/v1/files/upload_part' and value is None:value={}
                if type(value) is not dict:raise ValueError()
                return value
            except (httpx.ConnectTimeout,httpx.ConnectError):
                # Connect/TLS failures precede request delivery. Retry this one
                # unsent request only, never an ambiguous POST/part or full upload.
                if attempt==0:time.sleep(1);continue
                raise BackupError('cloud_connect_unavailable_request_not_sent') from None
            except httpx.HTTPError as exc:
                if method=='GET' and attempt==0:time.sleep(.3);continue
                raise BackupError(('cloud_submission_unknown_'+type(exc).__name__) if method!='GET' else 'cloud_read_unavailable') from None
            except (ValueError,KeyError,TypeError):raise BackupError('cloud_submission_unknown' if method!='GET' else 'cloud_response_invalid') from None
        raise BackupError('cloud_read_unavailable')

    def children(self,folder):
        valid_token(folder);rows=[];page='';seen=set()
        for _ in range(31):
            data=self.request('GET','/drive/v1/files',params={'folder_token':folder,'page_size':200,**({'page_token':page} if page else {})})
            files=data.get('files')
            if not isinstance(files,list) or any(type(x) is not dict for x in files):raise BackupError('cloud_directory_response_invalid')
            rows+=files
            if len(rows)>MAX_LIST:raise BackupError('cloud_directory_limit')
            if not data.get('has_more'):return rows
            page=data.get('next_page_token')
            if not isinstance(page,str) or not page or len(page)>4096 or page in seen:raise BackupError('cloud_pagination_invalid')
            seen.add(page)
        raise BackupError('cloud_directory_limit')

    def find(self,folder,name,kind):
        matches=[x for x in self.children(folder) if x.get('name')==name and x.get('type')==kind]
        if len(matches)>1:raise BackupError('cloud_duplicate_name_requires_review')
        return valid_token(matches[0].get('token')) if matches else ''

    def download(self,file_token,dest,limit,deadline):
        valid_token(file_token);url=API+'/drive/v1/files/'+file_token+'/download';access,_=token(self.http,path=self.credentials)
        for redirect in range(4):
            try:
                with self.http.stream('GET',url,headers={'Authorization':'Bearer '+access} if urlsplit(url).hostname=='open.feishu.cn' else {}) as response:
                    if response.status_code in {301,302,303,307,308}:
                        new=urljoin(url,response.headers.get('location',''));u=urlsplit(new)
                        if (u.scheme!='https' or not u.hostname or not (u.hostname=='open.feishu.cn' or u.hostname.endswith('.feishu.cn')) or u.username or u.password or u.port not in (None,443)):raise BackupError('cloud_download_redirect_rejected')
                        url=new;continue  # Signed temporary URLs never stored or logged; auth never forwarded cross-host.
                    if response.status_code in {401,403}:raise BackupError('cloud_scope_or_resource_permission_missing')
                    if response.status_code!=200:raise BackupError('cloud_download_unavailable')
                    h=hashlib.sha256();size=0
                    for chunk in response.iter_bytes(1024*1024):
                        if time.monotonic()>deadline:raise BackupError('io_deadline')
                        size+=len(chunk)
                        if size>limit:raise BackupError('cloud_download_size_mismatch')
                        h.update(chunk)
                        if dest is not None:dest.write(chunk)
                    return h.hexdigest(),size
            except httpx.HTTPError:raise BackupError('cloud_download_unavailable') from None
        raise BackupError('cloud_download_redirect_limit')

    def upload(self,folder,name,stream,size,deadline):
        if not 0<size<=MAX_FILE:raise BackupError('cloud_file_size_unsupported')
        fields={'file_name':name,'parent_type':'explorer','parent_node':valid_token(folder),'size':str(size)}
        if time.monotonic()>deadline:raise BackupError('io_deadline')
        if size<=MULTIPART_THRESHOLD:
            data=self.request('POST','/drive/v1/files/upload_all',data=fields,files={'file':(name,stream,'application/octet-stream')})
            return valid_token(data.get('file_token'))
        prepared=self.request('POST','/drive/v1/files/upload_prepare',json={**fields,'size':size})
        upload_id=prepared.get('upload_id');block=prepared.get('block_size');count=prepared.get('block_num')
        if not isinstance(upload_id,str) or not upload_id or len(upload_id)>4096 or type(block) is not int or not 64*1024<=block<=20*1024*1024 or type(count) is not int or count!=math.ceil(size/block):raise BackupError('cloud_submission_unknown')
        for seq in range(count):
            if time.monotonic()>deadline:raise BackupError('io_deadline')
            chunk=stream.read(min(block,size-seq*block))
            if len(chunk)!=min(block,size-seq*block):raise BackupError('source_changed')
            self.request('POST','/drive/v1/files/upload_part',data={'upload_id':upload_id,'seq':str(seq),'size':str(len(chunk)),
                'checksum':str(zlib.adler32(chunk)&0xffffffff)},files={'file':('part',chunk,'application/octet-stream')})
        data=self.request('POST','/drive/v1/files/upload_finish',json={'upload_id':upload_id,'block_num':count})
        return valid_token(data.get('file_token'))


class FeishuDriveStore:
    def __init__(self,root,store_id='',*,initialize=False,api=None):
        self.host,self.base_token=root_parts(root);self.base=root.rstrip('/');self.api=api or FeishuAPI()
        self.root_key=hashlib.sha256((self.api.app_id+':'+self.base_token).encode()).hexdigest()
        self.store_id='';self.objects='';self.snapshots=''
        try:
            with self.lock():
                marker=self.api.find(self.base_token,'creatorhub-store.json','file')
                if not marker:
                    if not initialize:raise BackupError('cloud_store_marker_missing')
                    if any(x.get('name') not in {'objects','snapshots'} for x in self.api.children(self.base_token)):raise BackupError('cloud_destination_not_empty')
                    # Deterministic identity survives a lost upload reply, but changing
                    # the root/app never silently points an old snapshot elsewhere.
                    identity=hashlib.sha256(('creatorhub:'+self.root_key).encode()).hexdigest()[:32]
                    raw=packed({'format':FORMAT,'store_id':identity,'provider':'feishu_drive','app_id':self.api.app_id})
                    marker,_=self.put_once(self.base_token,'creatorhub-store.json',io.BytesIO(raw),len(raw),hashlib.sha256(raw).hexdigest(),time.monotonic()+60)
                raw=self.read_small(marker,4096)
                try:m=json.loads(raw)
                except ValueError:raise BackupError('cloud_store_marker_invalid') from None
                if m.get('format')!=FORMAT or m.get('app_id')!=self.api.app_id or not re.fullmatch('[a-f0-9]{32}',str(m.get('store_id',''))):raise BackupError('cloud_store_marker_invalid')
                self.mark_existing(self.base_token,'creatorhub-store.json',marker,hashlib.sha256(raw).hexdigest(),len(raw))
                self.store_id=m['store_id']
                if store_id and store_id!=self.store_id:raise BackupError('destination_identity_changed')
                self.objects=self.folder('objects',initialize);self.snapshots=self.folder('snapshots',initialize)
        except BaseException:
            self.api.close();raise

    def close(self):self.api.close()

    def available_bytes(self):return None  # Quota is unknown, never invent free capacity.
    def same_filesystem(self,roots):return False

    @contextmanager
    def lock(self):
        from .hub_adapter import main_module
        main = main_module()
        from .video_backup import fcntl,directory
        if fcntl is None:raise BackupError('cloud_lock_requires_posix')
        p=Path(main.cfg.db_path).absolute().parent/'cloud-storage-locks';p.mkdir(mode=0o700,exist_ok=True);directory(p)
        fd=os.open(p/(self.root_key+'.lock'),os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise BackupError('storage_busy') from None
            yield
        finally:os.close(fd)

    def logical_name(self,folder,name):
        return name.split('.')[0] if folder==self.objects and re.fullmatch(r'[a-f0-9]{64}\.[a-z0-9]+',name) else name

    def receipt(self,folder,name):
        name=self.logical_name(folder,name)
        key=hashlib.sha256((self.root_key+'\0'+folder+'\0'+name).encode()).hexdigest()
        with get_session() as s:return s.get(CloudStorageWrite,key)

    def write_record(self,folder,name,kind,size,checksum):
        logical=self.logical_name(folder,name)
        key=hashlib.sha256((self.root_key+'\0'+folder+'\0'+logical).encode()).hexdigest()
        with get_session() as s:
            s.connection().exec_driver_sql('BEGIN IMMEDIATE')
            old=s.get(CloudStorageWrite,key)
            if old:
                if old.expected_sha256!=checksum or old.size!=size or old.kind!=kind:raise BackupError('cloud_immutable_key_conflict')
                raise BackupError('cloud_submission_unknown' if old.status in {'unknown','running'} else 'cloud_write_requires_review')
            row=CloudStorageWrite(id=key,root_key=self.root_key,folder_token=folder,name=name,kind=kind,size=size,expected_sha256=checksum)
            s.add(row);s.commit();return key

    def finish(self,key,file_token='',error=''):
        with get_session() as s:
            row=s.get(CloudStorageWrite,key)
            if not row:raise BackupError('cloud_write_receipt_missing')
            row.file_token=file_token;row.status=('rejected' if error in {'cloud_file_size_limit','cloud_tenant_quota_exceeded','cloud_user_quota_exceeded','cloud_folder_node_limit'} else 'unknown') if error else 'done';row.error_code=error;row.updated_at=datetime.utcnow();s.add(row);s.commit()

    def mark_existing(self,folder,name,token,checksum,size):
        row=self.receipt(folder,name)
        if row:
            if row.expected_sha256!=checksum or row.size!=size:raise BackupError('cloud_immutable_key_conflict')
            if row.status!='done' or row.file_token!=token:self.finish(row.id,token)

    def folder(self,name,initialize):
        found=self.api.find(self.base_token,name,'folder')
        if found:
            self.mark_existing(self.base_token,name,found,'',0);return found
        if not initialize:raise BackupError('cloud_store_layout_missing')
        key=self.write_record(self.base_token,name,'folder',0,'')
        try:
            data=self.api.request('POST','/drive/v1/files/create_folder',json={'folder_token':self.base_token,'name':name})
            found=valid_token(data.get('token'));self.finish(key,found);return found
        except BackupError as e:self.finish(key,error=str(e));raise

    def read_small(self,token,limit):
        b=io.BytesIO();self.api.download(token,b,limit,time.monotonic()+60);return b.getvalue()

    def put_once(self,folder,name,stream,size,checksum,deadline):
        found=self.api.find(folder,name,'file')
        if found:
            h,n=self.api.download(found,None,size,deadline)
            if h!=checksum or n!=size:raise BackupError('cloud_object_corrupt')
            self.mark_existing(folder,name,found,checksum,size);return found,True
        key=self.write_record(folder,name,'file',size,checksum)
        try:
            found=self.api.upload(folder,name,stream,size,deadline)
            # Success means remote bytes verified, not just a returned file token.
            h,n=self.api.download(found,None,size,deadline)
            if h!=checksum or n!=size:raise BackupError('cloud_object_corrupt')
            self.finish(key,found);return found,False
        except BackupError as e:self.finish(key,error=str(e));raise

    def object_token(self,checksum):
        if not re.fullmatch('[a-f0-9]{64}',checksum):raise BackupError('invalid_object_digest')
        rows=[x for x in self.api.children(self.objects) if x.get('type')=='file' and x.get('name','').startswith(checksum+'.')]
        if len(rows)>1:raise BackupError('cloud_duplicate_name_requires_review')
        return valid_token(rows[0].get('token')) if rows else ''

    def put_source(self,root,entry,deadline):
        with open_source(root,entry['relative']) as f:
            before=os.fstat(f.fileno())
            if (before.st_size,before.st_mtime_ns)!=(entry['size'],entry['mtime_ns']):raise BackupError('source_changed')
            checksum,size=stream_hash(f,deadline);f.seek(0)
            found=self.object_token(checksum)
            if found:
                self.verify_object(checksum,size,deadline)
                after=os.fstat(f.fileno())
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise BackupError('source_changed')
                self.mark_existing(self.objects,checksum+'.bin',found,checksum,size);return checksum,True
            ext=Path(entry['relative']).suffix.lower()
            if ext not in {'.png','.jpg','.jpeg','.webp','.mp4','.m4v','.mov','.mkv','.webm'}:ext='.bin'
            _,old=self.put_once(self.objects,checksum+ext,f,size,checksum,deadline)
            after=os.fstat(f.fileno())
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise BackupError('source_changed')
            return checksum,old

    def put_manifest(self,snapshot_id,data):
        if not re.fullmatch(ID_RE,snapshot_id) or not 0<len(data)<=MAX_MANIFEST:raise BackupError('manifest_invalid')
        self.put_once(self.snapshots,snapshot_id+'.json',io.BytesIO(data),len(data),hashlib.sha256(data).hexdigest(),time.monotonic()+60)

    def read_manifest(self,snapshot_id):
        if not re.fullmatch(ID_RE,snapshot_id):raise BackupError('manifest_invalid')
        found=self.api.find(self.snapshots,snapshot_id+'.json','file')
        if not found:raise BackupError('manifest_unavailable')
        return self.read_small(found,MAX_MANIFEST)

    def verify_object(self,checksum,size,deadline):
        found=self.object_token(checksum)
        if not found:raise BackupError('cloud_object_missing')
        h,n=self.api.download(found,None,size,deadline)
        if h!=checksum or n!=size:raise BackupError('object_corrupt')

    def restore_object(self,checksum,size,dest,deadline):
        found=self.object_token(checksum)
        if not found:raise BackupError('cloud_object_missing')
        with dest.open('xb') as f:
            os.chmod(dest,0o600);h,n=self.api.download(found,f,size,deadline);f.flush();os.fsync(f.fileno())
        if h!=checksum or n!=size:raise BackupError('object_corrupt')


def recover():
    with get_session() as s:
        rows=s.exec(select(CloudStorageWrite).where(CloudStorageWrite.status=='running')).all()
        for row in rows:row.status='unknown';row.error_code='service_restarted_remote_result_unknown';s.add(row)
        s.commit();return len(rows)
