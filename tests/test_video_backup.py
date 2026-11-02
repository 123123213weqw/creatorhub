"""REQ-014/015/030 technical rehearsal; NOT signed-off AC-014 on real storage."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
from unittest.mock import Mock

import pytest
from sqlmodel import select
import app.main as main
import app.db as db
import app.video_backup as vb
from app.models import LocalMediaAsset,VideoBackupOperation,CommentRule,ShareDownloadRecord
from test_project_optimizations import local_project,store
from test_access_and_submissions import client


@pytest.fixture
def project(local_project,monkeypatch):
    root=local_project.root
    main.cfg.db_path=str(local_project.path)
    monkeypatch.setattr(main,'UPLOAD_DIR',root/'uploads')
    for name in ['media','uploads','target','restore']:(root/name).mkdir(exist_ok=True)
    (root/'media/first.mp4').write_bytes(b'fixture video bytes v1')
    (root/'media/duplicate.mov').write_bytes(b'fixture video bytes v1')
    (root/'media/cookie.json').write_text('DO_NOT_BACK_UP_SECRET')
    (root/'media/profiles').mkdir();(root/'media/profiles/private.mp4').write_bytes(b'PRIVATE')
    return root


async def post(http,op,body=None,key=None):
    key=key or 'video-'+op+'-test-000001'
    return await http.post('/api/video-backups/'+op,json=body or {},headers={'Idempotency-Key':key})


async def configured(http,root):
    r=await post(http,'config',{'provider':'local_directory','root':str(root/'target')})
    assert r.status_code==200,r.text


async def execute(http,kind,body=None,key=None):
    r=await post(http,kind,body,key);assert r.status_code==200,r.text
    row=(await http.get('/api/video-backups/'+r.json()['id'])).json()
    return row


def test_unconfigured_fail_closed_no_target_no_network(project):
    async def run():
        async with client() as http:
            state=(await http.get('/api/video-backups/status')).json()
            assert not state['configured'] and state['provider']=='unconfigured'
            assert not state['automatic_schedule_enabled'] and state['inventory']['video_files']==2
            assert (await post(http,'snapshot')).status_code==409
            assert not (project/'target/creatorhub-video-backups').exists()
    asyncio.run(run())


def test_snapshot_deduplicates_and_is_versioned_not_mirroring(project):
    async def run():
        async with client() as http:
            await configured(http,project)
            a=await execute(http,'snapshot')
            assert a['status']=='done',a
            assert a['result']['video_files']==2 and a['result']['reused_files']==1
            assert a['result']['same_filesystem'] and not a['result']['independent_backup_verified']
            objects=project/'target/creatorhub-video-backups/objects';assert len(list(objects.iterdir()))==1
            # Reusing a request ID never schedules another copy.
            r=await post(http,'snapshot');assert r.json()['replayed'] and r.json()['id']==a['id']
            (project/'media/first.mp4').write_bytes(b'fixture video v2')
            b=await execute(http,'snapshot',key='video-snapshot-second-001')
            assert b['status']=='done' and len(list(objects.iterdir()))==2
            (project/'media/first.mp4').unlink();(project/'media/duplicate.mov').unlink()
            verified=await execute(http,'verify',{'snapshot_id':a['id']})
            assert verified['status']=='done' and len(list(objects.iterdir()))==2
    asyncio.run(run())


def test_restore_original_version_and_catalog_rules_without_overwrite(project):
    path=project/'media/first.mp4'
    store(LocalMediaAsset(id='asset_'+'a'*32,label='Video sample',media_type='video',manifest_json=json.dumps([{'path':str(path)}])))
    store(CommentRule(name='Representative business rule',templates='["fixture reply"]',xsec_token='DO_NOT_COPY_AUTH_TOKEN',enabled=False))
    store(ShareDownloadRecord(title='Linked download',files_json=json.dumps([{'path':str(path),'role':'media'}]),source_url='https://example.invalid/?token=DO_NOT_EXPORT_URL'))
    async def run():
        async with client() as http:
            await configured(http,project);snapshot=await execute(http,'snapshot')
            shutil.rmtree(project/'media') # disposable isolated fixture, never live user data
            r=await execute(http,'restore',{'snapshot_id':snapshot['id'],'restore_parent':str(project/'restore')})
            assert r['status']=='done',r
            dest=Path(r['result']['restore_path'])
            assert (dest/'downloads/first.mp4').read_bytes()==b'fixture video bytes v1'
            catalog=json.loads((dest/'catalog.json').read_text());assert catalog['assets'][0]['id']=='asset_'+'a'*32
            assert catalog['share_downloads'][0]['title']=='Linked download'
            assert 'DO_NOT_EXPORT_URL' not in json.dumps(catalog)
            rules=(dest/'rules.json').read_text();assert 'DO_NOT_BACK_UP_SECRET' not in rules and 'DO_NOT_COPY_AUTH_TOKEN' not in rules
            assert 'Representative business rule' in rules and json.loads(rules)['restore_requires_admin_review']
            assert r['result']['rules_and_catalog_restored'] and not r['result']['live_data_overwritten']
            assert not (project/'media').exists()
            again=await execute(http,'restore',{'snapshot_id':snapshot['id'],'restore_parent':str(project/'restore')},'second-restore-request-001')
            assert again['status']=='done' and again['result']['restore_path']!=str(dest)
    asyncio.run(run())


@pytest.mark.parametrize('corrupt',['object','manifest'])
def test_corruption_is_failure_not_success_or_silent_repair(project,corrupt):
    async def run():
        async with client() as http:
            await configured(http,project);snapshot=await execute(http,'snapshot')
            store_path=project/'target/creatorhub-video-backups'
            path=next((store_path/'objects').iterdir()) if corrupt=='object' else store_path/'snapshots'/(snapshot['id']+'.json')
            path.write_bytes(b'corrupt')
            verify=await execute(http,'verify',{'snapshot_id':snapshot['id']})
            assert verify['status']=='failed' and verify['error_code']==corrupt+'_corrupt'
            restore=await execute(http,'restore',{'snapshot_id':snapshot['id'],'restore_parent':str(project/'restore')})
            assert restore['status']=='failed' and not list((project/'restore').iterdir())
    asyncio.run(run())


def test_destination_missing_or_identity_changed_is_not_recreated(project):
    async def run():
        async with client() as http:
            await configured(http,project)
            p=project/'target/creatorhub-video-backups';p.rename(project/'target/offline-store')
            assert (await post(http,'snapshot')).status_code==409
            assert not p.exists()
            (project/'target/offline-store').rename(p)
            (p/'store.json').write_text(json.dumps({'format':vb.FORMAT,'store_id':'b'*32}))
            assert (await post(http,'snapshot')).status_code==409
    asyncio.run(run())


def test_no_symlinks_or_destination_inside_source(project):
    (project/'media/alias.mp4').symlink_to(project/'media/first.mp4')
    roots,files,summary=vb.inventory();assert summary['video_files']==2 and summary['skipped_files']==1
    with pytest.raises((vb.BackupError,OSError)):
        with vb.open_source(project/'media','alias.mp4'):pass
    async def run():
        async with client() as http:
            for root in [project/'media',project,Path('/')]:
                assert (await post(http,'config',{'provider':'local_directory','root':str(root)},'bad-target-'+str(len(str(root)))+'-000001')).status_code==422
    asyncio.run(run())


def test_low_space_and_size_limit_preserve_sources(project,monkeypatch):
    monkeypatch.setattr(vb.LocalDirectoryStore,'available_bytes',lambda _:0)
    async def run():
        async with client() as http:
            await configured(http,project);r=await execute(http,'snapshot')
            assert r['status']=='failed' and r['error_code']=='insufficient_space'
            assert (project/'media/first.mp4').exists()
    asyncio.run(run())


def test_service_restart_marks_interrupted_never_replays(project):
    oid=store(VideoBackupOperation(id='vbak_'+'a'*32,request_hash='a',payload_hash='b',kind='snapshot',status='running'))
    assert vb.recover()==1
    with db.get_session() as s:assert s.get(VideoBackupOperation,oid).status=='interrupted'


def test_zero_local_videos_not_false_success(project):
    (project/'media/first.mp4').unlink();(project/'media/duplicate.mov').unlink()
    async def run():
        async with client() as http:
            await configured(http,project);r=await execute(http,'snapshot')
            assert r['status']=='failed' and r['error_code']=='no_local_videos'
    asyncio.run(run())


def test_missing_source_storage_not_zero_and_not_new_snapshot_success(project):
    (project/'uploads').rmdir()
    async def run():
        async with client() as http:
            await configured(http,project)
            status=(await http.get('/api/video-backups/status')).json()
            assert not status['inventory']['complete'] and status['inventory']['unavailable_sources']==['uploads']
            result=await execute(http,'snapshot')
            assert result['status']=='failed' and result['error_code']=='source_storage_unavailable'
    asyncio.run(run())


def test_source_changes_during_copy_fail_and_no_manifest(project,monkeypatch):
    original=vb.LocalDirectoryStore.put_source
    def changed(self,root,entry,deadline):
        (root/entry['relative']).write_bytes(b'concurrent edit')
        return original(self,root,entry,deadline)
    monkeypatch.setattr(vb.LocalDirectoryStore,'put_source',changed)
    async def run():
        async with client() as http:
            await configured(http,project);r=await execute(http,'snapshot')
            assert r['status']=='failed' and r['error_code']=='source_changed'
            assert not list((project/'target/creatorhub-video-backups/snapshots').iterdir())
    asyncio.run(run())


def test_restore_path_traversal_rejected():
    for path in ['../x','/tmp/x','a/../../x','a\\b','']:
        with pytest.raises(vb.BackupError):vb.relpath(path)
