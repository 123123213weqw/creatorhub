"""Cloud contract/failure tests use only fake HTTP/remote objects; no grants/sends."""
import hashlib,io,json,os,time,uuid
from pathlib import Path
from unittest.mock import Mock
import httpx,pytest
from sqlmodel import select
import app.main as main
import app.db as db
from app.models import VideoBackupConfig,CloudStorageWrite
from app.video_backup import BackupError
from app import feishu_drive as fd
from test_project_optimizations import local_project
from test_video_backup import project,post,execute
from test_access_and_submissions import client

ROOT_URL='https://sample.feishu.cn/drive/folder/folderRoot0001'

class FakeRemote:
    app_id='cli_fixture0001'
    def __init__(self):self.folders={'folderRoot0001':[]};self.bytes={};self.posts=[];self.lose_reply=False;self.unreadable=False
    def close(self):pass
    def children(self,folder):
        if self.unreadable:raise BackupError('cloud_scope_or_resource_permission_missing')
        return list(self.folders.get(folder,[]))
    def find(self,folder,name,kind):
        rows=[r for r in self.children(folder) if r['name']==name and r['type']==kind]
        if len(rows)>1:raise BackupError('cloud_duplicate_name_requires_review')
        return rows[0]['token'] if rows else ''
    def request(self,method,path,**kw):
        assert method=='POST' and path=='/drive/v1/files/create_folder'
        data=kw['json'];token='fld_'+uuid.uuid4().hex;self.folders[token]=[]
        self.folders[data['folder_token']].append({'token':token,'name':data['name'],'type':'folder'});self.posts.append(('folder',data['name']));return {'token':token}
    def download(self,token,dest,limit,deadline):
        if self.unreadable:raise BackupError('cloud_scope_or_resource_permission_missing')
        raw=self.bytes[token]
        if len(raw)>limit:raise BackupError('cloud_download_size_mismatch')
        if dest is not None:dest.write(raw)
        return hashlib.sha256(raw).hexdigest(),len(raw)
    def upload(self,folder,name,stream,size,deadline):
        data=stream.read();assert len(data)==size;token='file_'+uuid.uuid4().hex;self.bytes[token]=data
        self.folders[folder].append({'token':token,'name':name,'type':'file'});self.posts.append(('file',name))
        if self.lose_reply:self.lose_reply=False;raise BackupError('cloud_submission_unknown')
        return token

@pytest.fixture
def remote(local_project):
    main.cfg.db_path=str(local_project.path)
    return FakeRemote()

@pytest.mark.parametrize('value',['http://sample.feishu.cn/drive/folder/token0001','https://sample.feishu.cn.evil/drive/folder/token0001','https://u:p@sample.feishu.cn/drive/folder/token0001','https://sample.feishu.cn/drive/folder/token0001?secret=x','https://sample.feishu.cn:8443/drive/folder/token0001','/tmp/folder','https://127.0.0.1/drive/folder/token0001'])
def test_cloud_root_is_not_arbitrary_url(value):
    with pytest.raises(BackupError):fd.root_parts(value)


def test_immutable_version_restore_dedupe_and_no_delete(remote,local_project):
    store=fd.FeishuDriveStore(ROOT_URL,initialize=True,api=remote)
    f=local_project.root/'demo.mp4';f.write_bytes(b'fixture video bytes')
    entry={'relative':f.name,'size':f.stat().st_size,'mtime_ns':f.stat().st_mtime_ns}
    h,old=store.put_source(f.parent,entry,time.monotonic()+30);assert not old
    assert store.put_source(f.parent,entry,time.monotonic()+30)==(h,True)
    raw=b'{"fixture":true}';sid='vbak_'+'a'*32;store.put_manifest(sid,raw);assert store.read_manifest(sid)==raw
    f.unlink();dest=f.parent/'isolated.mp4';store.restore_object(h,entry['size'],dest,time.monotonic()+30);assert dest.read_bytes()==b'fixture video bytes'
    assert store.available_bytes() is None and store.same_filesystem({}) is False
    assert not hasattr(store,'delete') and not hasattr(store.api,'delete')
    second=fd.FeishuDriveStore(ROOT_URL,store.store_id,api=remote);assert second.store_id==store.store_id
    assert len([p for p in remote.posts if p[0]=='folder'])==2


def test_unknown_upload_reconciles_existing_bytes_without_repost(remote,local_project):
    s=fd.FeishuDriveStore(ROOT_URL,initialize=True,api=remote)
    f=local_project.root/'one.mp4';f.write_bytes(b'fixture video');entry={'relative':f.name,'size':f.stat().st_size,'mtime_ns':f.stat().st_mtime_ns}
    remote.lose_reply=True
    with pytest.raises(BackupError,match='unknown'):s.put_source(f.parent,entry,time.monotonic()+30)
    posts=len(remote.posts)
    with db.get_session() as sess:assert sess.exec(select(CloudStorageWrite).where(CloudStorageWrite.status=='unknown')).first()
    h,old=s.put_source(f.parent,entry,time.monotonic()+30);assert old and len(remote.posts)==posts
    with db.get_session() as sess:assert not sess.exec(select(CloudStorageWrite).where(CloudStorageWrite.status=='unknown')).all()


def test_unknown_not_visible_never_reposts_even_if_extension_changes(remote,local_project):
    s=fd.FeishuDriveStore(ROOT_URL,initialize=True,api=remote)
    f=local_project.root/'one.mp4';f.write_bytes(b'fixture video');entry={'relative':f.name,'size':f.stat().st_size,'mtime_ns':f.stat().st_mtime_ns}
    remote.lose_reply=True
    with pytest.raises(BackupError):s.put_source(f.parent,entry,time.monotonic()+30)
    saved=remote.folders[s.objects];remote.folders[s.objects]=[];posts=len(remote.posts)
    f.rename(f.with_suffix('.mov'));f=f.with_suffix('.mov');entry['relative']=f.name
    with pytest.raises(BackupError,match='unknown'):s.put_source(f.parent,entry,time.monotonic()+30)
    assert len(remote.posts)==posts
    remote.folders[s.objects]=saved


def test_corrupt_objects_revocation_and_store_identity_fail_closed(remote,local_project):
    s=fd.FeishuDriveStore(ROOT_URL,initialize=True,api=remote)
    f=local_project.root/'one.mp4';f.write_bytes(b'fixture video');entry={'relative':f.name,'size':f.stat().st_size,'mtime_ns':f.stat().st_mtime_ns}
    h,_=s.put_source(f.parent,entry,time.monotonic()+30);token=s.object_token(h);remote.bytes[token]=b'corrupt'
    with pytest.raises(BackupError,match='corrupt'):s.verify_object(h,entry['size'],time.monotonic()+30)
    with pytest.raises(BackupError,match='identity_changed'):fd.FeishuDriveStore(ROOT_URL,'f'*32,api=remote)
    remote.unreadable=True
    with pytest.raises(BackupError,match='permission'):fd.FeishuDriveStore(ROOT_URL,s.store_id,api=remote)


def test_do_not_initialize_nonempty_user_folder(remote):
    remote.folders['folderRoot0001'].append({'name':'not-our-private-document','type':'file','token':'file_other'})
    with pytest.raises(BackupError,match='not_empty'):fd.FeishuDriveStore(ROOT_URL,initialize=True,api=remote)
    assert not remote.posts


def test_cloud_needs_separate_business_upload_authorization(project,monkeypatch):
    import asyncio
    from app import video_backup as vb
    fake=Mock(base=ROOT_URL,store_id='a'*32)
    monkeypatch.setattr(vb,'make_store',Mock(return_value=fake))
    async def run():
        async with client() as h:
            r=await post(h,'config',{'provider':'feishu_drive','root':ROOT_URL});assert r.status_code==200,r.text
            assert r.json()['cloud_upload_authorized'] is False
            r=await post(h,'snapshot');assert r.status_code==409 and '未授权' in r.text
            with db.get_session() as s:assert not s.exec(select(vb.VideoBackupOperation)).all()
    asyncio.run(run())

@pytest.fixture
def credentials(tmp_path):
    p=tmp_path/'credentials.json';p.write_text(json.dumps({'app_id':'cli_fixture0001','app_secret':'fixture-not-real-secret'}));p.chmod(0o600)
    fd.TOKEN_CACHE.clear();return p


def test_tokens_cached_refreshed_and_never_on_drive_request_body(credentials):
    calls=[]
    def handle(request):
        calls.append(request)
        if request.url.path.endswith('/internal'):return httpx.Response(200,json={'code':0,'tenant_access_token':'fixture-bearer-value','expire':7200})
        assert b'fixture-not-real-secret' not in request.content
        return httpx.Response(200,json={'code':0,'data':{'files':[],'has_more':False}})
    api=fd.FeishuAPI(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=credentials)
    assert api.children('folderRoot0001')==[] and api.children('folderRoot0001')==[]
    assert len([r for r in calls if r.url.path.endswith('/internal')])==1
    fd.TOKEN_CACHE.clear();api.children('folderRoot0001');assert len([r for r in calls if r.url.path.endswith('/internal')])==2
    credentials.chmod(0o644)
    with pytest.raises(BackupError,match='private'):api.children('folderRoot0001')


def test_small_and_multipart_upload_contract(credentials):
    calls=[]
    def handle(request):
        calls.append(request)
        if request.url.path.endswith('/internal'):data={'code':0,'tenant_access_token':'fixture-bearer-value','expire':7200}
        elif request.url.path.endswith('/upload_prepare'):data={'code':0,'data':{'upload_id':'upload_fixture','block_size':4*1024*1024,'block_num':6}}
        elif request.url.path.endswith('/upload_part'):data={'code':0,'data':None}
        else:data={'code':0,'data':{'file_token':'file_fixture'}}
        return httpx.Response(200,json=data)
    api=fd.FeishuAPI(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=credentials)
    assert api.upload('folderRoot0001','fixture.mp4',io.BytesIO(b'abc'),3,time.monotonic()+30)=='file_fixture'
    raw=b'0'*(21*1024*1024);assert api.upload('folderRoot0001','large.mp4',io.BytesIO(raw),len(raw),time.monotonic()+30)=='file_fixture'
    assert len([r for r in calls if r.url.path.endswith('/upload_all')])==1
    assert len([r for r in calls if r.url.path.endswith('/upload_part')])==6
    part=next(r for r in calls if r.url.path.endswith('/upload_part'))
    assert b'name="checksum"' in part.content and b'name="seq"' in part.content and b'name="file"' in part.content


def test_post_timeout_never_auto_replayed_and_read_failure_not_zero(credentials):
    counts={'post':0,'get':0}
    def handle(r):
        if r.url.path.endswith('/internal'):return httpx.Response(200,json={'code':0,'tenant_access_token':'fixture-bearer','expire':7200})
        counts[r.method.lower()]+=1;raise httpx.ReadTimeout('fixture lost reply')
    api=fd.FeishuAPI(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=credentials)
    with pytest.raises(BackupError,match='submission_unknown'):api.upload('folderRoot0001','x.mp4',io.BytesIO(b'abc'),3,time.monotonic()+30)
    assert counts['post']==1
    with pytest.raises(BackupError,match='read_unavailable'):api.children('folderRoot0001')
    assert counts['get']==2


def test_download_redirect_does_not_leak_auth_and_signed_query_not_logged(credentials,caplog):
    seen=[]
    def handle(r):
        seen.append(r)
        if r.url.path.endswith('/internal'):return httpx.Response(200,json={'code':0,'tenant_access_token':'fixture-bearer','expire':7200})
        if r.url.host=='open.feishu.cn':return httpx.Response(302,headers={'location':'https://internal-api-drive-stream.feishu.cn/file?code=SIGNED_SECRET'})
        assert 'authorization' not in r.headers
        return httpx.Response(200,content=b'fixture',headers={'content-type':'application/octet-stream'})
    caplog.set_level('INFO',logger='httpx')
    api=fd.FeishuAPI(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=credentials)
    h,n=api.download('file_fixture',None,100,time.monotonic()+30);assert n==7 and h==hashlib.sha256(b'fixture').hexdigest()
    assert 'SIGNED_SECRET' not in caplog.text and 'fixture-bearer' not in caplog.text
    assert len(seen)==3


def test_external_download_redirect_rejected(credentials):
    def handle(r):
        if r.url.path.endswith('/internal'):return httpx.Response(200,json={'code':0,'tenant_access_token':'fixture-bearer','expire':7200})
        return httpx.Response(302,headers={'location':'https://evil.example/steal'})
    api=fd.FeishuAPI(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=credentials)
    with pytest.raises(BackupError,match='redirect_rejected'):api.download('file_fixture',None,100,time.monotonic()+30)


def test_explicit_quota_rejection_is_not_generic_or_retried(credentials):
    calls=[]
    def handle(r):
        if r.url.path.endswith('/internal'):return httpx.Response(200,json={'code':0,'tenant_access_token':'fixture-bearer','expire':7200})
        calls.append(r);return httpx.Response(400,json={'code':1061043,'msg':'file size beyond limit'})
    api=fd.FeishuAPI(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=credentials)
    with pytest.raises(BackupError,match='cloud_file_size_limit'):api.upload('folderRoot0001','x.mp4',io.BytesIO(b'x'*100),100,time.monotonic()+30)
    assert len(calls)==1


def test_only_unsent_connect_failure_gets_bounded_post_retry(credentials):
    calls=[]
    def handle(r):
        if r.url.path.endswith('/internal'):return httpx.Response(200,json={'code':0,'tenant_access_token':'fixture-bearer','expire':7200})
        calls.append(r)
        if len(calls)==1:raise httpx.ConnectTimeout('no connection/no request sent')
        return httpx.Response(200,json={'code':0,'data':{'file_token':'file_fixture'}})
    api=fd.FeishuAPI(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=credentials)
    assert api.upload('folderRoot0001','x.mp4',io.BytesIO(b'abc'),3,time.monotonic()+30)=='file_fixture'
    assert len(calls)==2


def test_full_api_cloud_snapshot_verify_restore_with_private_business(remote,project,monkeypatch):
    import asyncio,shutil
    from app import video_backup as vb
    from app.models import LocalMediaAsset
    from app.business_models import BusinessContent,ContentVersion
    from test_project_optimizations import store as save
    image=project/'uploads/input.png';image.write_bytes(b'fixture image only')
    aid='asset_'+'a'*32;cid='content_'+'a'*32
    save(LocalMediaAsset(id=aid,label='fixture',media_type='images',is_demo=True,manifest_json=json.dumps([{'path':str(image),'size':image.stat().st_size,'sha256':hashlib.sha256(image.read_bytes()).hexdigest(),'name':image.name}])))
    save(BusinessContent(id=cid,label='fixture',owner_actor_id='local_owner'))
    save(ContentVersion(id='version_'+'a'*32,content_id=cid,title='fixture',asset_ids_json=json.dumps([aid]),digest='a'*64,assets_digest='b'*64,revision=1))
    monkeypatch.setattr(vb,'make_store',lambda provider,root,store_id='',initialize=False:fd.FeishuDriveStore(root,store_id,initialize=initialize,api=remote))
    async def run():
        async with client() as h:
            r=await post(h,'config',{'provider':'feishu_drive','root':ROOT_URL,'cloud_upload_authorized':True});assert r.status_code==200,r.text
            a=await execute(h,'snapshot');assert a['status']=='done',a
            assert a['result']['business_evidence_files']==1
            count=len(remote.posts)
            v=await execute(h,'verify',{'snapshot_id':a['id']});assert v['status']=='done',v
            assert len(remote.posts)==count
            shutil.rmtree(project/'media')
            b=await execute(h,'restore',{'snapshot_id':a['id'],'restore_parent':str(project/'restore')});assert b['status']=='done',b
            dest=Path(b['result']['restore_path'])
            assert (dest/'downloads/first.mp4').read_bytes()==b'fixture video bytes v1'
            assert (dest/'business-assets/input.png').read_bytes()==b'fixture image only'
            assert b['result']['live_data_overwritten'] is False
            assert b'fixture-not-real-secret' not in (dest/'business.json').read_bytes()
    asyncio.run(run())


def test_running_cloud_receipt_recovers_unknown_without_replay(remote):
    key='a'*64
    with db.get_session() as s:
        s.add(CloudStorageWrite(id=key,root_key='r',folder_token='fixture_folder',name='fixture',kind='file'));s.commit()
    assert fd.recover()==1 and not remote.posts
    with db.get_session() as s:assert s.get(CloudStorageWrite,key).status=='unknown'
