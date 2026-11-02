import unittest
from creatorhub_cli.client import Client,Error


class Fake:
    def __init__(self):self.calls=[]
    def get(self,url):
        self.calls.append(('GET',url))
        if url.endswith('/status'):return {'configured':False,'provider':'unconfigured','root':'/Users/private/secret','inventory':{'video_files':0,'bytes':0},'last_success':None}
        return {'id':'vbak_'+'a'*32,'status':'done','kind':'restore','result':{'restore_path':'/Users/private/secret','isolated_restore':True,'video_files':2}}
    def post(self,url,body,key):
        self.calls.append(('POST',url,body,key))
        return {'id':'vbak_'+'b'*32,'status':'pending','kind':'snapshot','result':{}}


class BackupTests(unittest.TestCase):
    def test_status_unconfigured_is_not_backup_success(self):
        f=Fake();r=Client(transport=f).call('backup.status')
        self.assertFalse(r['configured']);self.assertIsNone(r['last_success'])
        self.assertNotIn('/Users',str(r));self.assertEqual(f.calls[0][0],'GET')

    def test_cloud_connection_is_distinct_from_business_authorization(self):
        class Cloud(Fake):
            def get(self,url):return {'configured':True,'provider':'feishu_drive','implemented_providers':['local_directory','feishu_drive','https://PRIVATE'],'destination_available':True,'cloud_upload_authorized':False,'external_io':True,'cloud_quota_available_bytes':None,'root':'https://private.feishu.cn/drive/folder/PRIVATE','app_secret':'PRIVATE','inventory':{'video_files':3}}
        r=Client(transport=Cloud()).call('backup.status')
        self.assertTrue(r['configured']);self.assertFalse(r['cloud_upload_authorized']);self.assertTrue(r['external_io'])
        self.assertEqual(r['implemented_providers'],['local_directory','feishu_drive']);self.assertIsNone(r['cloud_quota_available_bytes']);self.assertNotIn('PRIVATE',str(r))

    def test_create_has_one_fixed_post_and_explicit_stable_key(self):
        f=Fake();r=Client(transport=f).call('backup.create',{'request_key':'my-video-backup-00001'})
        self.assertEqual(len(f.calls),1);self.assertEqual(f.calls[0][1],'http://127.0.0.1:18080/api/video-backups/snapshot')
        self.assertEqual(r['status'],'pending');self.assertNotIn('done',r.values())

    def test_restore_is_isolated_and_does_not_expose_paths(self):
        f=Fake();r=Client(transport=f).call('backup.show',{'operation_id':'vbak_'+'a'*32})
        self.assertTrue(r['result']['isolated_restore']);self.assertNotIn('restore_path',str(r))
        Client(transport=f).call('backup.restore',{'snapshot_id':'vbak_'+'a'*32,'request_key':'restore-video-0000001'})
        self.assertEqual(f.calls[-1][2],{'snapshot_id':'vbak_'+'a'*32})

    def test_bad_paths_unknown_fields_no_restore_overwrite_or_prune(self):
        for extra in [{'restore_parent':'https://external.example'},{'overwrite':True},{'delete_missing':True},{'token':'secret'}]:
            with self.assertRaises(Error):Client(transport=Fake()).call('backup.restore',{'snapshot_id':'vbak_'+'a'*32,'request_key':'restore-video-0000001',**extra})
        with self.assertRaises(Error):Client(transport=Fake())._post('/api/video-backups/config',{},'fixed-request-00001')

    def test_incomplete_write_response_is_unknown_no_retries(self):
        for result in [{},{'id':'vbak_'+'a'*32,'status':'pending','result':None}]:
            class Broken(Fake):
                def post(self,*args):self.calls.append(args);return result
            f=Broken()
            with self.assertRaises(Error) as e:Client(transport=f).call('backup.verify',{'snapshot_id':'vbak_'+'a'*32,'request_key':'verify-video-00000001'})
            self.assertEqual(e.exception.code,'submission_unknown');self.assertEqual(len(f.calls),1)


if __name__=='__main__':unittest.main()
