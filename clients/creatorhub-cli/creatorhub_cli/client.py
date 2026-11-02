from __future__ import annotations

from datetime import datetime,timezone
import json
import os
import uuid
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class Error(Exception):
    def __init__(self,code,message,exit_code=2):
        self.code,self.message,self.exit_code=code,message,exit_code
        super().__init__(message)


def require(ok,code,message,exit_code=2):
    if not ok:raise Error(code,message,exit_code)


def field(type_,**extra):return {"type":type_,**extra}
REQUEST_KEY=field("string",pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$",maxLength=128)
BACKUP_ID=field("string",pattern=r"^vbak_[a-f0-9]{32}$")
PUBLISH_FIELDS=['id','platform','account_id','account_name','media_type','title','desc','topics','location','status','visibility','allow_save','error','scheduled_at','next_allowed_at','blocked_reason','created_at','demo_only','review_required','review_version','review_digest','approved_at','media_count','replayed']
SYNC_FIELDS=['id','platform','account_id','kind','work_id','status','attempts','fetched','added','blocked_reason','error','next_allowed_at','created_at','started_at','finished_at','replayed','deduplicated']
PLATFORM=field("string",enum=["all","douyin","xhs","kuaishou","shipinhao","tiktok"])
PAGE={"page":field("integer",minimum=1,maximum=10000),"page_size":field("integer",minimum=1,maximum=100)}
ACCOUNT={"account_id":field("integer",minimum=1)}
CATALOG={
    "doctor":({},[]),"capabilities":({},[]),"schema":({},[]),
    "accounts.list":({"platform":PLATFORM,**PAGE},[]),
    "overview":({"platform":PLATFORM},[]),
    "works.list":({**ACCOUNT,**PAGE},["account_id"]),
    "comments.list":({**ACCOUNT,"work_id":field("integer",minimum=1),**PAGE},["account_id","work_id"]),
    "stats.show":({**ACCOUNT},["account_id"]),
    "tasks.list":({"platform":PLATFORM,"state":field("string",enum=["active","pending","running","blocked","failed","completed","all"]),**PAGE},[]),
    "collections.results":({"platform":PLATFORM,"job_id":field("integer",minimum=1),**PAGE},[]),
    "publish.list":({"platform":PLATFORM,**PAGE},[]),
    "risk.status":({"platform":PLATFORM},[]),
    "sync.request":({**ACCOUNT,"kind":field("string",enum=["works","comments"]),"work_id":field("integer",minimum=1),"request_key":REQUEST_KEY},["account_id","kind","request_key"]),
    "sync.status":({"task_id":field("integer",minimum=1)},["task_id"]),
    "publish.draft":({**ACCOUNT,"request_key":REQUEST_KEY,
        "media_type":field("string",enum=["images","video"]),
        "media_paths":field("array",items=field("string",maxLength=4096),minItems=1,maxItems=9),
        "asset_ids":field("array",items=field("string",pattern=r"^asset_[a-f0-9]{32}$"),minItems=1,maxItems=9),
        "demo_only":field("boolean"),
        "title":field("string",maxLength=20),"desc":field("string",maxLength=10000),
        "topics":field("string",maxLength=1000),"location":field("string",maxLength=60),
        "visibility":field("string",enum=["public","friends","private"]),
        "allow_save":field("boolean"),"scheduled_at":field("string",maxLength=80)},
        ["account_id","request_key","media_type"]),
    "publish.status":({"task_id":field("integer",minimum=1)},["task_id"]),
    "assets.list":({},[]),
    "assets.register":({"label":field("string",maxLength=80),"media_type":field("string",enum=["images","video"]),
        "media_paths":field("array",items=field("string",maxLength=4096),minItems=1,maxItems=9),"is_demo":field("boolean"),"request_key":REQUEST_KEY},["label","media_type","media_paths","request_key"]),
    "leads.demo":({**ACCOUNT,"request_key":REQUEST_KEY},["account_id","request_key"]),
    "leads.propose":({**ACCOUNT,"work_id":field("integer",minimum=1),"request_key":REQUEST_KEY},["account_id","work_id","request_key"]),
    "leads.list":({**ACCOUNT,**PAGE,"state":field("string",enum=["candidate","approved","rejected","recorded_mock"])},[]),
    "leads.status":({"lead_id":field("integer",minimum=1)},["lead_id"]),
    "crm.preview":({"lead_id":field("integer",minimum=1)},["lead_id"]),
    "crm.mock":({"lead_id":field("integer",minimum=1),"version":field("integer",minimum=1),"digest":field("string",pattern=r"^[a-f0-9]{64}$"),"request_key":REQUEST_KEY},["lead_id","version","digest","request_key"]),
    "requests.status":({"operation":field("string",enum=["publish","sync","asset","leads"]),"request_key":REQUEST_KEY},["operation","request_key"]),
    "backup.status":({},[]),"backup.list":({},[]),
    "backup.create":({"request_key":REQUEST_KEY},["request_key"]),
    "backup.show":({"operation_id":BACKUP_ID},["operation_id"]),
    "backup.verify":({"snapshot_id":BACKUP_ID,"request_key":REQUEST_KEY},["snapshot_id","request_key"]),
    "backup.restore":({"snapshot_id":BACKUP_ID,"restore_parent":field("string",maxLength=4096),"request_key":REQUEST_KEY},["snapshot_id","request_key"]),
}


from .operations import CATALOG as OPERATIONS_CATALOG, WRITE_COMMANDS as OPERATIONS_WRITES
CATALOG.update(OPERATIONS_CATALOG)
from .wecom import CATALOG as WECOM_CATALOG
CATALOG.update(WECOM_CATALOG)
from .extensions import CATALOG as EXTENSIONS_CATALOG, WRITES as EXTENSION_WRITES
CATALOG.update(EXTENSIONS_CATALOG)
from .wecom_moments import CATALOG as MOMENTS_CATALOG, WRITES as MOMENTS_WRITES
CATALOG.update(MOMENTS_CATALOG)
from .confirmed_actions import CATALOG as CONFIRMED_CATALOG, WRITES as CONFIRMED_WRITES
CATALOG.update(CONFIRMED_CATALOG)
WRITE_COMMANDS=CONFIRMED_WRITES|{'sync.request','publish.draft','assets.register','leads.demo','leads.propose','crm.mock','backup.create','backup.verify','backup.restore','wecom.refresh'}|OPERATIONS_WRITES
WRITE_COMMANDS |= EXTENSION_WRITES
WRITE_COMMANDS |= MOMENTS_WRITES


def validate(command,args):
    require(isinstance(command,str) and command in CATALOG,"unknown_command","命令不在现有 Hub 客户端白名单")
    require(type(args) is dict,"invalid_arguments","参数必须是 JSON 对象")
    props,required=CATALOG[command]
    require(set(required)<=set(args) and not set(args)-set(props),"invalid_arguments","缺少必填参数或含未知参数")
    def check(v,p):
        if v is None and p.get('nullable') is True:return
        kind=p["type"]
        if isinstance(kind,list):
            if v is None and 'null' in kind:return
            kind=next(k for k in kind if k!='null')
        if kind=="integer":require(type(v) is int and p.get("minimum",1)<=v<=p.get("maximum",2**31-1),"invalid_integer","整数参数超出范围")
        elif kind=="boolean":require(type(v) is bool,"invalid_boolean","布尔参数必须为 true 或 false")
        elif kind=="object":
            require(type(v) is dict and set(p.get('required',[]))<=set(v) and not set(v)-set(p.get('properties',{})),"invalid_object","对象字段不正确")
            for k,x in v.items():check(x,p['properties'][k])
        elif kind=="array":
            require(type(v) is list and p.get("minItems",0)<=len(v)<=p.get("maxItems",100),"invalid_array","数组数量不正确")
            for item in v:check(item,p['items'])
        else:
            require(isinstance(v,str),"invalid_string","字符串参数格式不正确")
            if 'enum' in p:require(v in p['enum'],"invalid_enum","枚举值不正确")
            require(len(v)<=p.get('maxLength',10000),"invalid_string","字符串超过长度限制")
            if 'pattern' in p:require(re.fullmatch(p['pattern'],v) is not None,"invalid_request_key","提交编号须为 16–128 位字母、数字或 . _ : -")
    for k,v in args.items():check(v,props[k])
    if command=='backup.restore' and args.get('restore_parent'):
        require(Path(args['restore_parent']).is_absolute() and not args['restore_parent'].startswith('//'),'invalid_restore_path','恢复只接受本机绝对目录；NAS需预先由管理员挂载',3)
    if command=='sync.request':
        require(('work_id' in args)==(args['kind']=='comments'),'work_binding','评论同步必须指定 work_id；作品同步不接受 work_id')
    if command=='publish.draft':
        require(bool(args.get('media_paths')) != bool(args.get('asset_ids')),'media_selection','media_paths 与 asset_ids 必须且只能选择一种')
    if command in {'publish.draft','assets.register'}:
        require(all(Path(p).is_absolute() and not p.startswith('//') for p in args.get('media_paths',[])),'invalid_media_path','素材只接受本机绝对路径，不接受 URL 或网络路径',3)
        require(args['media_type']!='video' or len(args.get('media_paths',args.get('asset_ids',[])))==1,'invalid_array','视频草稿只接受一个文件')
        if args.get('scheduled_at'):
            try:when=datetime.fromisoformat(args['scheduled_at'].replace('Z','+00:00'))
            except ValueError:raise Error('invalid_schedule','预约时间须为带时区的 ISO 时间') from None
            require(when.tzinfo is not None,'invalid_schedule','预约时间须包含时区；不传则确认后尽快发布')
    return args


def clean(v):
    if isinstance(v,str):
        v=re.sub(r"(?i)(?:bearer\s+\S+|sk-[a-z0-9_-]{16,})","[凭证隐藏]",v)
        v=re.sub(r"(?i)\b(?:cookie|authorization|[a-z_]*token|[a-z_]*secret|api[_-]?key|password)\s*[:=]\s*\S+","[凭证隐藏]",v)
        v=re.sub(r"https?://\S+","[链接隐藏]",v)
        v=re.sub(r"/(?:Users|home|tmp|var|data)/\S+","[路径隐藏]",v)
        return ''.join(c for c in v[:4000] if ord(c)>=32 or c in '\n\t')
    require(v is None or type(v) in (bool,int,float),"invalid_response","Hub 字段格式不正确",5)
    return v


def pick(row,fields):
    require(type(row) is dict,"invalid_response","Hub 记录格式不正确",5)
    return {k:clean(row[k]) for k in fields if k in row}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class Transport:
    def __init__(self):
        self.token=os.environ.get('HUBCTL_OPERATIONS_TOKEN','')
        require(not self.token or re.fullmatch(r'cht_[A-Za-z0-9_-]{20,100}',self.token),'invalid_team_token','团队凭据格式不正确',3)
    def headers(self,base):
        return dict(base,**({'Authorization':'Bearer '+self.token} if self.token else {}))
    def get(self,url):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        for attempt in range(2):
            try:
                with opener.open(urllib.request.Request(url,headers=self.headers({"Accept":"application/json"}),method="GET"),timeout=10) as r:
                    raw=r.read(2*1024*1024+1)
                require(len(raw)<=2*1024*1024,"response_too_large","Hub 响应超过上限",5)
                return json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code in {502,503,504} and attempt==0:time.sleep(.4);continue
                raise Error("hub_http_"+str(e.code),"Hub 拒绝读取；不能将失败解释为零",5) from None
            except (OSError,TimeoutError,ValueError,urllib.error.URLError):
                if attempt==0:time.sleep(.4);continue
                raise Error("hub_unavailable","Hub 读取失败，未启动其他服务或替代队列",5) from None

    def post(self,url,body,key):
        # NEVER retry writes automatically, even 502/timeout may hide a commit.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        raw=json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
        request=urllib.request.Request(url,data=raw,method="POST",headers=self.headers({
            "Accept":"application/json","Content-Type":"application/json","Idempotency-Key":key}))
        try:
            with opener.open(request,timeout=60) as response:
                raw=response.read(2*1024*1024+1)
            require(len(raw)<=2*1024*1024,"submission_unknown","提交结果未确认；保留原 request_key 和输入，沿用编号重试后查状态",5)
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code>=500:raise Error("submission_unknown","提交结果未确认；沿用原 request_key 和完全相同输入重试，不要换编号",5) from None
            raise Error("hub_http_"+str(e.code),"Hub 拒绝提交；请核对输入、版本或队列，不会自动重试",5) from None
        except (OSError,TimeoutError,ValueError,urllib.error.URLError):
            raise Error("submission_unknown","提交结果未确认；沿用原 request_key 和完全相同输入重试，不要换编号",5) from None


    def multipart(self,url,path,fields,key):
        p=Path(path)
        require(p.is_file() and not p.is_symlink() and 0<p.stat().st_size<=8*1024*1024,'invalid_proof_file','截图必须是普通文件且最多8MiB',3)
        raw=p.read_bytes();require(0<len(raw)<=8*1024*1024,'invalid_proof_file','截图超过8MiB',3)
        boundary='hubctl'+uuid.uuid4().hex
        require(all(boundary.encode() not in x.encode() for x in fields.values()) and boundary.encode() not in raw,'multipart_boundary','截图或说明与传输边界冲突，请核对输入',3)
        data=b''
        for name,value in fields.items():data+=(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
        data+=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="proof"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()+raw+(f'\r\n--{boundary}--\r\n').encode()
        request=urllib.request.Request(url,data=data,method='POST',headers=self.headers({'Accept':'application/json','Content-Type':'multipart/form-data; boundary='+boundary,'Idempotency-Key':key}))
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=60) as response:out=response.read(2*1024*1024+1)
            require(len(out)<=2*1024*1024,'submission_unknown','凭证回执过大，沿用原编号核对',5)
            return json.loads(out)
        except urllib.error.HTTPError as e:
            raise Error('submission_unknown' if e.code>=500 else 'hub_http_'+str(e.code),'凭证提交未成功确认；核对原目标状态，勿盲目换编号',5) from None
        except (OSError,ValueError,urllib.error.URLError):raise Error('submission_unknown','凭证提交结果未知；保留同一编号、文件和说明核对',5) from None


def configured_url(path=None):
    if path is None:return "http://127.0.0.1:18080"
    p=Path(path).expanduser()
    require(p.is_file() and not p.is_symlink() and p.stat().st_mode&0o077==0,"private_config","配置须为权限 600 的普通文件",3)
    data=json.loads(p.read_text())
    require(type(data) is dict and set(data)=={"hub_url"},"invalid_config","CLI 配置仅允许 hub_url，不存登录态或密钥")
    return data["hub_url"]


class Client:
    def __init__(self,hub_url="http://127.0.0.1:18080",transport=None):
        require(isinstance(hub_url,str),"hub_boundary","Hub 地址格式不正确",3)
        self.url=hub_url.rstrip('/')
        u=urllib.parse.urlsplit(self.url)
        try:port=u.port
        except ValueError:port=None
        require(u.scheme=='http' and u.hostname in {'127.0.0.1','localhost','::1'} and port
                and not any((u.path,u.query,u.fragment,u.username,u.password)),"hub_boundary","只允许显式本机回环 Hub 地址",3)
        self.transport=transport or Transport()

    def _get(self,path,params=None):
        allowed=(self._moments_allowed(path,False) or self._operations_allowed(path,False) or path in {'/health','/api/accounts','/api/overview/summary','/api/account-works','/api/task-queue','/api/publish','/api/collections','/api/risk-control/summary','/api/local-assets','/api/leads','/api/video-backups','/api/video-backups/status','/api/integrations/wecom/status','/api/integrations/wecom/business/status'}
                 or re.fullmatch(r'/api/integrations/wecom/business/requests/[A-Za-z0-9][A-Za-z0-9._:-]{15,127}',path)
                 or re.fullmatch(r'/api/integrations/wecom/requests/[A-Za-z0-9][A-Za-z0-9._:-]{15,127}',path)
                 or re.fullmatch(r'/api/video-backups/vbak_[a-f0-9]{32}',path)
                 or re.fullmatch(r'/api/account-works/[1-9][0-9]*/comments',path)
                 or re.fullmatch(r'/api/(publish|sync-requests|leads)/[1-9][0-9]*',path)
                 or re.fullmatch(r'/api/leads/[1-9][0-9]*/crm-preview',path)
                 or re.fullmatch(r'/api/client-receipts/(publish|sync|asset|leads)/[A-Za-z0-9][A-Za-z0-9._:-]{15,127}',path)
                 or re.fullmatch(r'/api/account-stats/[1-9][0-9]*',path)
                 or re.fullmatch(r'/api/collections/[1-9][0-9]*',path)
                 or re.fullmatch(r'/api/collections/[1-9][0-9]*/contents',path))
        require(allowed,"route_not_allowed","接口不在固定 GET 白名单",3)
        return self.transport.get(self.url+path+('?' +urllib.parse.urlencode(params) if params else ''))

    def _post(self,path,body,key):
        require(getattr(self,'_confirmed_active',False) and self._confirmed_allowed(path) or self._moments_allowed(path,True) or self._operations_allowed(path,True) or path in {'/api/sync-requests','/api/publish/drafts','/api/local-assets','/api/leads/propose','/api/leads/demo','/api/video-backups/snapshot','/api/video-backups/verify','/api/video-backups/restore','/api/integrations/wecom/refresh','/api/integrations/wecom/business/refresh'} or re.fullmatch(r'/api/leads/[1-9][0-9]*/crm-mock',path),'route_not_allowed','接口不在固定提交白名单',3)
        return self.transport.post(self.url+path,body,key)

    @staticmethod
    def _confirmed_allowed(path):
        return bool(re.fullmatch(r'/api/publish/[1-9][0-9]*/approve',path) or re.fullmatch(r'/api/integrations/wecom/moments/wecom_moment_[a-f0-9]{32}/(review|submit)',path))

    @staticmethod
    def _moments_allowed(path,write):
        root='/api/integrations/wecom/moments'
        if write:
            return path in {root+'/preflight',root+'/drafts'} or bool(re.fullmatch(root+r'/wecom_moment_[a-f0-9]{32}/refresh',path))
        return path in {root,root+'/binding'} or bool(re.fullmatch(root+r'/(wecom_moment_[a-f0-9]{32}|receipts/(draft|preflight)/[A-Za-z0-9][A-Za-z0-9._:-]{15,127})',path))

    @staticmethod
    def _operations_allowed(path,write):
        if not path.startswith('/api/operations/'):return False
        tail=path[len('/api/operations/'):]
        if write:
            if tail in {'observations','delivery/preflight'}:return True
            return tail in {'contents','plans','distribution','delivery/leads','manual-publishes'} or bool(re.fullmatch(r'(contents/content_[a-f0-9]{32}/versions|versions/version_[a-f0-9]{32}/variants|accounts/[1-9][0-9]*/persona|plans/plan_[a-f0-9]{32}/drafts|distribution/distribution_[a-f0-9]{32}/change|targets/target_[a-f0-9]{32}/claim)',tail))
        return tail in {'session','actors','readiness','health','reports','contents','personas','plans','distribution','delivery','manual-publishes','observations','delivery/preflight','handoff','delivery-profile','backup-policy'} or bool(re.fullmatch(r'(distribution/distribution_[a-f0-9]{32}(/materials)?|proofs/proof_[a-f0-9]{32}|observations/observation_[a-f0-9]{32})',tail))

    @staticmethod
    def _rows(raw):
        require(isinstance(raw,list) and all(type(r) is dict for r in raw),"invalid_response","Hub 列表格式不正确",5)
        return raw

    @staticmethod
    def _page(rows,args,cap=None):
        page,size=args.get('page',1),args.get('page_size',20)
        return {'items':rows[(page-1)*size:page*size],'total_in_snapshot':len(rows),'page':page,'page_size':size,
                'more_may_exist':bool(cap and len(rows)>=cap)}

    def call(self,command,args=None):
        args=validate(command,{} if args is None else args)
        if getattr(self.transport,'token','') and command not in OPERATIONS_CATALOG and command not in {'schema','capabilities'}:raise Error('team_scope','团队凭据仅允许业务工作流命令；不回退本机管理员',3)
        if command=='schema':return {'commands':{n:{'type':'object','properties':p,'required':r,'additionalProperties':False} for n,(p,r) in CATALOG.items()}}
        if command=='capabilities':return {'version':'0.12.0','interface':'cli_only','console_url':self.url,
            'state_authority':'existing_creatorhub','independent_database':False,'independent_queue':False,
            'independent_approval':False,'background_worker':False,'write_commands':sorted(WRITE_COMMANDS),'human_confirmation':'owner_exact_snapshot','publish_approval_command':True, 'commands':sorted(CATALOG)}
        if command=='doctor':
            health=self._get('/health');require(type(health) is dict,"invalid_response","健康响应不正确",5)
            status=health.get('status',health.get('ok'))
            require(status in ('ok','healthy',True),'hub_unhealthy','Hub 未报告健康状态',5)
            business=self._get('/api/operations/readiness') if health.get('business_operations_protocol')=='1.0' else {}
            require(type(business) is dict,'invalid_response','业务就绪响应不正确',5)
            return {'hub_reachable':True,'health':status,'console_url':self.url,'state_authority':'existing_creatorhub','new_service_started':False,'hub_cli_protocol':health.get('hub_cli_protocol'),'write_extension_ready':health.get('hub_cli_protocol')=='1.0',
                    'local_assets_ready':health.get('local_assets') is True,'offline_leads_ready':health.get('offline_leads') is True,
                    'crm_mode':health.get('crm_mode','unavailable'),'real_crm_enabled':business.get('crm_enabled',False),'business_operations_ready':health.get('business_operations_protocol')=='1.0','team_gateway_enabled':business.get('remote_team_gateway_enabled',False),'video_backup_ready':health.get('video_backup_protocol')=='1.0','wecom_readonly_ready':health.get('wecom_readonly_protocol')=='1.0','wecom_moments_ready':health.get('wecom_moments_protocol')=='1.0'}
        if command in CONFIRMED_CATALOG:
            from .confirmed_actions import call
            self._confirmed_active=command in CONFIRMED_WRITES
            try:return call(self,command,args)
            finally:self._confirmed_active=False
        if command in OPERATIONS_CATALOG:
            from .operations import call
            return call(self,command,args)
        if command in WECOM_CATALOG:
            from .wecom import call
            return call(self,command,args)
        if command in EXTENSIONS_CATALOG:
            from .extensions import call
            return call(self,command,args)
        if command in MOMENTS_CATALOG:
            from .wecom_moments import call
            return call(self,command,args)
        if command.startswith('backup.'):return self._backup(command,args)
        if command in {'assets.list','assets.register','leads.demo','leads.propose','leads.list','leads.status','crm.preview','crm.mock','requests.status'}:
            return self._workflow(command,args)
        if command in {'sync.request','publish.draft'}:
            body={k:v for k,v in args.items() if k!='request_key'}
            path='/api/sync-requests' if command=='sync.request' else '/api/publish/drafts'
            raw=self._post(path,body,args['request_key'])
            require(type(raw) is dict and type(raw.get('id')) is int,'submission_unknown','Hub 回执不完整；沿用原编号核对，勿新建提交',5)
            if command=='publish.draft':require(raw.get('status')=='draft' and raw.get('review_required') is True and re.fullmatch(r'[a-f0-9]{64}',str(raw.get('review_digest',''))),'submission_unknown','草稿回执契约不正确；沿用原编号核对，不能当作已创建草稿',5)
            result=pick(raw,SYNC_FIELDS if command=='sync.request' else PUBLISH_FIELDS)
            return {**result,'request_key':args['request_key'],'state_authority':'existing_creatorhub',
                    'platform_action_triggered_by_cli':False,
                    'execution':'hub_controlled_read_queue' if command=='sync.request' else 'human_confirmation_required_in_hub',
                    'console_url':self.url+('/#queue' if command=='sync.request' else '/#publish')}
        if command in {'sync.status','publish.status'}:
            raw=self._get(('/api/sync-requests/' if command=='sync.status' else '/api/publish/')+str(args['task_id']))
            result=pick(raw,SYNC_FIELDS if command=='sync.status' else PUBLISH_FIELDS)
            return {**result,'source':'existing_hub_database','state_authority':'existing_creatorhub',
                    'platform_refresh_triggered':False,'read_at':datetime.now(timezone.utc).isoformat()}
        platform=args.get('platform','all');params={'platform':platform} if platform!='all' else {}
        if command=='accounts.list':
            rows=[pick(r,['id','platform','nickname','status','has_read_login','has_creator','follower_count']) for r in self._rows(self._get('/api/accounts',params))]
            result=self._page(rows,args)
        elif command=='overview':
            platforms=[platform] if platform!='all' else ['douyin','xhs','kuaishou','shipinhao','tiktok']
            result={'platforms':[pick(self._get('/api/overview/summary',{'platform':p}),['platform','accounts','monitors','downloaded','comments']) for p in platforms]}
        elif command=='works.list':
            rows=self._work_rows(args['account_id'])
            result=self._page([pick(r,['id','account_id','platform','item_id','title','desc','create_time','like_count','comment_count','play_count','share_count','fetched_at']) for r in rows],args,500)
        elif command=='comments.list':
            works=self._work_rows(args['account_id'])
            work=next((r for r in works if r.get('id')==args['work_id'] and r.get('account_id',args['account_id'])==args['account_id']),None)
            require(work is not None,'work_account_mismatch','作品不在指定账号的可见快照中',3)
            raw=self._rows(self._get(f"/api/account-works/{args['work_id']}/comments",{'limit':300}))
            result=self._page([pick(r,['id','platform','comment_id','text','user_nickname','user_sec_uid','create_time','created_at','like_count']) for r in raw],args,300)
            result.update(account_id=args['account_id'],work_id=args['work_id'])
        elif command=='stats.show':
            raw=self._get(f"/api/account-stats/{args['account_id']}")
            require(type(raw) is dict,'invalid_response','统计响应不正确',5)
            result={'trend':[pick(r,['date','follower_count','aweme_count','total_like','total_comment','total_play']) for r in self._rows(raw.get('trend',[]))[:180]],
                    'works':[pick(r,['id','account_id','platform','item_id','title','like_count','comment_count','play_count','share_count','fetched_at']) for r in self._rows(raw.get('works',[]))[:100]],
                    'caveat':'字段按 Hub 已存快照返回；无权限、无快照与数值为零并不相同。'}
        elif command=='tasks.list':
            raw=self._get('/api/task-queue',{**params,'state':args.get('state','active'),'page':args.get('page',1),'page_size':args.get('page_size',20)})
            result=pick(raw,['total','page','page_size','pages','generated_at'])
            result['items']=[pick(r,['key','id','queue_type','queue_label','platform','account_id','account_name','title','state','status','error','blocked_reason','scheduled_at','created_at']) for r in self._rows(raw.get('items',[]))[:args.get('page_size',20)]]
            if isinstance(raw.get('summary'),dict):result['summary']=pick(raw['summary'],['total','active','pending','running','blocked','failed','completed'])
        elif command=='publish.list':
            raw=self._rows(self._get('/api/publish',params))
            result=self._page([pick(r,['id','platform','account_id','title','desc','status','error','scheduled_at','next_allowed_at','blocked_reason','created_at','schedule_needs_confirmation']) for r in raw],args)
        elif command=='collections.results':
            if args.get('job_id'):
                if platform!='all':
                    meta=self._get(f"/api/collections/{args['job_id']}")
                    require(type(meta) is dict and meta.get('platform')==platform,'platform_mismatch','采集任务不属于所选平台',3)
                raw=self._get(f"/api/collections/{args['job_id']}/contents",{'page':args.get('page',1),'page_size':args.get('page_size',20)})
                result=pick(raw,['total','page','page_size','pages'])
                result['items']=[pick(r,['id','platform','aweme_id','desc','author_name','comment_count','create_time']) for r in self._rows(raw.get('items',[]))[:args.get('page_size',20)]]
            else:
                rows=self._rows(self._get('/api/collections',{**params,'limit':100}))
                result=self._page([pick(r,['id','platform','account_id','status','content_count','comment_count','created_at']) for r in rows],args,100)
        elif command=='risk.status':
            raw=self._get('/api/risk-control/summary',params)
            result=pick(raw,['total','abnormal','risk_events_today','blocked_tasks','policy_enabled','mode','sampled_at'])
            result['counts']=pick(raw.get('counts',{}),['normal','cooldown','recovering','auth_invalid','proxy_error','network_circuit','write_paused','verification_required','network_backoff'])
        else:raise Error('unknown_command','命令未实现')
        return {**result,'source':'existing_hub_database','read_at':datetime.now(timezone.utc).isoformat(),
                'platform_refresh_triggered':False,'state_authority':'existing_creatorhub'}

    def _backup(self,command,args):
        meta={'state_authority':'existing_creatorhub','platform_refresh_triggered':False,'platform_write_triggered':False,'storage_io_only':True}
        def operation(row):
            d=pick(row,['id','kind','snapshot_id','status','error_code','manifest_sha256','created_at','finished_at','replayed'])
            d['result']=pick(row.get('result',{}),['video_files','bytes','video_bytes','business_evidence_bytes','new_object_bytes','reused_files','objects_verified','same_filesystem','independent_backup_verified','elapsed_seconds','isolated_restore','live_data_overwritten','rules_and_catalog_restored','rpo_target_evaluated','rto_target_evaluated','partial_files_may_exist','business_evidence_files','business_package_included','business_package_restored','credentials_reissued','role_grants_applied','external_events_replayed'])
            return d
        if command=='backup.status':
            raw=self._get('/api/video-backups/status')
            data=pick(raw,['configured','provider','destination_available','provider_interface_version','cloud_upload_authorized','external_io','cloud_file_limit_verified','cloud_quota_available_bytes','max_bytes','retention','automatic_schedule_enabled','independent_backup_verified','coverage','ac_014_customer_acceptance','error_code'])
            data['implemented_providers']=[x for x in raw.get('implemented_providers',[]) if x in {'local_directory','feishu_drive'}]
            data['inventory']=pick(raw.get('inventory',{}),['video_files','bytes','skipped_files','complete','platform_fetch_triggered'])
            data['inventory']['unavailable_sources']=[x for x in raw.get('inventory',{}).get('unavailable_sources',[]) if x in {'downloads','uploads'}]
            data['last_success']=operation(raw['last_success']) if raw.get('last_success') else None
            return {**meta,**data}
        if command=='backup.list':return {**meta,'items':[operation(r) for r in self._rows(self._get('/api/video-backups'))]}
        if command=='backup.show':return {**meta,**operation(self._get('/api/video-backups/'+args['operation_id']))}
        kind={'backup.create':'snapshot','backup.verify':'verify','backup.restore':'restore'}[command]
        raw=self._post('/api/video-backups/'+kind,{k:v for k,v in args.items() if k!='request_key'},args['request_key'])
        require(type(raw) is dict and re.fullmatch(r'vbak_[a-f0-9]{32}',str(raw.get('id',''))) and raw.get('status') in {'pending','running','done','failed','interrupted'} and type(raw.get('result',{})) is dict,'submission_unknown','备份操作回执不完整；保留原编号和输入核对，勿新建操作',5)
        try:receipt=operation(raw)
        except Error:raise Error('submission_unknown','备份操作回执格式异常；保留原编号核对，勿新建操作',5) from None
        return {**meta,**receipt,'request_key':args['request_key'],'execution':'existing_hub_storage_io','check_status_command':'hubctl backup show --operation-id '+raw['id']}

    def _workflow(self,command,args):
        meta={'state_authority':'existing_creatorhub','platform_refresh_triggered':False,'external_write_triggered':False}
        lead_fields=['id','platform','account_id','work_id','version','digest','state','owner_label','is_demo','created_at','reviewed_at','replayed']
        if command=='assets.list':
            return {**meta,'items':[pick(x,['id','label','media_type','is_demo','media_count','created_at']) for x in self._rows(self._get('/api/local-assets'))]}
        if command=='assets.register':
            body={k:v for k,v in args.items() if k!='request_key'}
            raw=self._post('/api/local-assets',body,args['request_key'])
            require(type(raw) is dict and re.fullmatch(r'asset_[a-f0-9]{32}',str(raw.get('id',''))) and type(raw.get('is_demo')) is bool and type(raw.get('media_count')) is int,'submission_unknown','素材提交回执不完整；沿用原编号核对',5)
            return {**meta,**pick(raw,['id','label','media_type','is_demo','media_count','created_at','replayed'])}
        if command=='leads.demo':
            raw=self._post('/api/leads/demo',{k:v for k,v in args.items() if k!='request_key'},args['request_key'])
            require(type(raw) is dict and type(raw.get('id')) is int and raw.get('is_demo') is True and raw.get('state') in {'candidate','approved','rejected','recorded_mock'},'submission_unknown','演示候选回执不完整；沿用原编号核对',5)
            return {**meta,**pick(raw,lead_fields)}
        if command=='leads.propose':
            raw=self._post('/api/leads/propose',{k:v for k,v in args.items() if k!='request_key'},args['request_key'])
            require(type(raw) is dict and all(type(raw.get(k)) is int and raw[k]>=0 for k in ('scanned','matched','created','updated')) and raw.get('analysis')=='local_explainable_rules' and raw.get('platform_refresh_triggered') is False,'submission_unknown','缓存分析回执不完整；沿用原编号核对',5)
            return {**meta,**pick(raw,['account_id','work_id','scanned','matched','created','updated','scan_limit','more_may_exist','analysis','customer_identity_verified','replayed'])}
        if command=='leads.list':
            raw=self._get('/api/leads',args)
            return {**meta,**pick(raw,['total_in_snapshot','more_may_exist','page','page_size']),'items':[pick(x,lead_fields) for x in self._rows(raw['items'])]}
        if command=='leads.status':
            raw=self._get(f"/api/leads/{args['lead_id']}")
            # Metadata only: full comment/contact review stays in the local Hub.
            return {**meta,**pick(raw,lead_fields)}
        if command=='requests.status':
            raw=self._get('/api/client-receipts/'+args['operation']+'/'+args['request_key'])
            return {**meta,**pick(raw,['operation','request_key','receipt_found','id','status','account_id','work_id','kind','created','updated','scanned','matched','is_demo','demo_only'])}
        if command=='crm.preview':
            raw=self._get(f"/api/leads/{args['lead_id']}/crm-preview")
            require(type(raw) is dict and raw.get('mode')=='local_mock' and raw.get('real_table_configured') is False and type(raw.get('fields')) is dict,'invalid_response','当前客户端仅支持本机模拟 CRM',5)
            return {**meta,**pick(raw,['mode','real_table_configured','owner_field_type']),
                    'field_names':[clean(x) for x in raw.get('fields',{})],'full_values_visible_in_hub_only':True}
        if command=='crm.mock':
            body={k:v for k,v in args.items() if k not in {'request_key','lead_id'}}
            raw=self._post(f"/api/leads/{args['lead_id']}/crm-mock",body,args['request_key'])
            require(type(raw) is dict and type(raw.get('id')) is int and type(raw.get('mock_record_id')) is int and raw.get('mode')=='local_mock' and raw.get('status')=='recorded_mock' and raw.get('external_write_triggered') is False and raw.get('real_table_configured') is False,'submission_unknown','模拟入表回执不完整；先核对候选状态，勿盲目重提',5)
            return {**meta,**pick(raw,['id','mock_record_id','mode','created','deduplicated','real_table_configured','status','replayed'])}

    def _work_rows(self,account_id):
        return self._rows(self._get('/api/account-works',{'account_id':account_id,'limit':500}))
