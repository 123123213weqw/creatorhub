"""Private-chat command adapter. Hub remains the sole business state authority.

LLM tools read metadata or prepare immutable confirmation intents. Only an
exact owner code in the same chat can execute a write. No shell or file paths.
"""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import os
import signal
import subprocess
import tempfile
import time

from hub import HubTools,HubError
from readonly_cli import NEW_MAP,validate as validate_read,safe_metadata
from actions import ActionStore,CATALOG as ACTIONS,WRITE_NAMES,WRITE_COMMANDS,REVIEW,validate as validate_action,digest
from tool_router import TOOLS,select as select_tools,DOMAINS

CLI=Path.home()/'.local/bin/hubctl'
CLI_RELEASE_ROOT=Path(os.environ.get('HUILIMA_CLI_RELEASE_ROOT',str(Path.home()/'.local/share/creatorhub-cli/releases'))).expanduser()
READ_MAP={'hub_overview':'overview','hub_accounts':'accounts.list','hub_tasks':'tasks.list','hub_collection_results':'collections.results'}
READ_MAP.update(NEW_MAP)
EXPLICIT_ONLY_READS={'sync.status','leads.list','leads.status','crm.preview','requests.status'}
WRITE_KINDS={'sync.request':'sync','publish.draft':'publish','leads.propose':'leads','crm.mock':'crm'}
EXTRA_READS={n:s['command'] for n,s in ACTIONS.items() if not s['write']}
CONFIRMED_ONLY={'publish.approve','wecom.moments-dispatch'}
ALLOWED_COMMANDS=set(READ_MAP.values())|EXPLICIT_ONLY_READS|set(WRITE_KINDS)|WRITE_COMMANDS|set(EXTRA_READS.values())|set(REVIEW.values())|{'wecom.moments-scope'}
HELP='''Hub CLI 私聊命令（仅白名单用户）
直接问：查看账号状态、最近发布任务、客户朋友圈任务状态、备份连接是否正常。
自然语言按需调用只读工具或生成写操作确认单。确认单绑定内容/账号/范围，10分钟失效；只有同一私聊精确确认码才能提交。
/账号
/作品 账号ID
/同步作品 账号ID
/同步评论 账号ID 作品ID
/同步状态 任务ID
/素材
/草稿 {"account_id":1,"media_type":"images","asset_ids":["asset_..."],"title":"标题","desc":"正文","visibility":"private"}
/发布状态 任务ID
/意向分析 账号ID 作品ID
/意向列表
/意向状态 候选ID
/CRM预览 候选ID
/CRM演练 候选ID
/核对 bot-sync-... 或 bot-publish-... 或 bot-leads-...
素材先由本机 hubctl assets register 导入。机器人不接受本机文件路径；可准备发布与企微派发，经精确确认码执行；不能批准线索，也不发送抖音评论或私信。
意向分析为本机规则，只读已有评论；完整原文和负责人在原 Hub「本账号管理→意向候选」复核。CRM演练只写 Hub 本机模拟记录，不代表真实飞书入表。
问答仍可使用 DeepSeek；上述显式命令不经过模型，评论原文/联系方式不交给模型。'''


class CLIBridge(HubTools):
    workflow_enabled=True
    tools=TOOLS
    tool_contract_version="0.12-confirmed-actions-v1"
    direct_external_write_tools=0
    write_preparation_count=len(WRITE_NAMES)
    def __init__(self,config,allowed_senders,audit_path=None,secrets=(),runner=None,actions_path=None):
        super().__init__(config,allowed_senders,audit_path,secrets=secrets)
        self.http.close() # inherited validator/redactor/audit only; no REST client used
        self.runner=runner or self._subprocess
        self.cli=CLI  # Resolve the installed launcher on each invocation, not a stale source checkout.
        self._checked_target=None
        self.cli_version=None
        self.request_hash=''
        self.message_id='';self.chat_id='';self.actor='';self._confirming=False;self.last_receipt=None
        self.actions=ActionStore(actions_path);self.prepared=[]
        if runner is None and not self.cli.is_file():raise ValueError('CLI is not installed')
        if self.base!='http://127.0.0.1:18080':
            raise ValueError('本机桥接当前固定连接现用18080，不读取模型指定配置')

    def begin_message(self,message_id,chat_id='',actor=''):
        self.request_hash=hashlib.sha256(message_id.encode()).hexdigest()[:16]
        self.message_id,self.chat_id,self.actor=message_id,chat_id,actor;self.prepared=[];self.last_receipt=None

    def _executable(self):
        try:target=self.cli.resolve(strict=True)
        except OSError:raise HubError('cli_not_installed') from None
        if not target.is_file() or not target.is_relative_to(CLI_RELEASE_ROOT.resolve()) or target.name!='hubctl':
            raise HubError('cli_launcher_boundary')
        if target!=self._checked_target:
            try:
                r=subprocess.run([str(target),'--version'],capture_output=True,text=True,timeout=5,
                    env={k:v for k,v in os.environ.items() if k not in {'HUBCTL_CONFIG','PYTHONPATH'}})
            except (OSError,subprocess.TimeoutExpired):raise HubError('cli_version_unavailable') from None
            version=r.stdout.strip()
            if r.returncode or not re.fullmatch(r'[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}',version):
                raise HubError('cli_version_invalid')
            if tuple(map(int,version.split('.'))) < (0,11,0):raise HubError('cli_upgrade_required')
            self._checked_target,self.cli_version=target,version
        return target

    def _subprocess(self,command,args):
        if command not in ALLOWED_COMMANDS:raise HubError('command_not_allowed')
        executable=self._executable()
        if command in WRITE_COMMANDS|set(REVIEW.values())|{'wecom.moments-scope'} and tuple(map(int,self.cli_version.split('.')))<(0,12,0):raise HubError('cli_upgrade_required')
        env={k:v for k,v in os.environ.items() if k not in {'HUBCTL_CONFIG','PYTHONPATH'}}
        with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
            p=subprocess.Popen([str(executable),*command.split('.'),'--input','-'],
                stdin=subprocess.PIPE,stdout=out,stderr=err,env=env,start_new_session=True)
            try:p.communicate(json.dumps(args,ensure_ascii=False).encode(),timeout=75)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid,signal.SIGTERM)
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
                return {'ok':False,'error':{'code':'submission_unknown' if command in set(WRITE_KINDS)|WRITE_COMMANDS else 'cli_timeout'}}
            out.seek(0);raw=out.read(1024*1024+1)
        if len(raw)>1024*1024:raise HubError('response_too_large')
        try:result=json.loads(raw)
        except (ValueError,UnicodeError):raise HubError('cli_invalid_response') from None
        if not isinstance(result,dict) or type(result.get('ok')) is not bool:raise HubError('cli_invalid_response')
        if p.returncode and result['ok']:raise HubError('cli_invalid_exit')
        return result

    def invoke(self,command,args,actor,message_id=''):
        if actor not in self.allowed_senders:raise HubError('not_authorized')
        if command not in ALLOWED_COMMANDS:raise HubError('command_not_allowed')
        if command in WRITE_COMMANDS-set(WRITE_KINDS) and not self._confirming:raise HubError('human_confirmation_required')
        key=None
        if command in WRITE_KINDS:
            if not message_id:raise HubError('trusted_message_id_required')
            if self._confirming:
                key=args.get('request_key','')
                if not re.fullmatch(r'bot-action-action_[a-f0-9]{32}',key):raise HubError('trusted_request_key_required')
            else:
                key='bot-'+WRITE_KINDS[command]+'-'+hashlib.sha256((actor+'\0'+message_id+'\0'+command).encode()).hexdigest()[:48]
            args={**args,'request_key':key}
        result=self.runner(command,args)
        if not isinstance(result,dict):raise HubError('cli_invalid_response')
        if self.audit:
            self.audit.info(json.dumps({'time':datetime.now(timezone.utc).isoformat(),
                'actor_hash':hashlib.sha256(actor.encode()).hexdigest()[:12],
                'request_hash':self.request_hash,'command':command,'cli_version':self.cli_version,
                'ok':result.get('ok',False),'error':result.get('error',{}).get('code')}))
        return result,key

    def execute(self,name,arguments,actor=''):
        result={'ok':False,'tool':name if name in READ_MAP else 'unknown','source':self.label,'fetched_at':datetime.now(timezone.utc).isoformat()}
        try:
            if actor not in self.allowed_senders:raise HubError('not_authorized')
            if name in WRITE_NAMES:return self.prepare_action(name,arguments,actor)
            if name=='hub_resolve_account':
                if type(arguments) is not dict or set(arguments)!={'query'} or type(arguments['query']) is not str or not 0<len(arguments['query'])<=80:raise HubError('invalid_arguments')
                raw,_=self.invoke('accounts.list',{'page_size':100},actor)
                if not raw.get('ok'):raise HubError('account_lookup_failed')
                rows=raw['data'].get('items',[]);matches=[x for x in rows if str(x.get('nickname','')).strip().casefold()==arguments['query'].strip().casefold()]
                result.update(ok=True,data={'unique':len(matches)==1,'matching_ids':[x['id'] for x in matches],'account_id':matches[0]['id'] if len(matches)==1 else None})
                return result
            args=validate_action(name,arguments) if name in EXTRA_READS else validate_read(name,arguments)
            command=READ_MAP.get(name) or EXTRA_READS.get(name)
            if command is None:raise HubError('tool_not_allowed')
            raw,_=self.invoke(command,args,actor)
            if not raw.get('ok'):
                code=raw.get('error',{}).get('code','cli_failed')
                if not isinstance(code,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}',code):code='cli_failed'
                raise HubError(code)
            data=safe_metadata(raw['data'])
            if command=='operations.readiness' and isinstance(raw['data'].get('result'),dict):
                original=raw['data']['result']
                for field,output in [('pending_external_inputs','pending_input_count'),('human_confirmation_required','human_confirmation_count')]:
                    if isinstance(original.get(field),list):data.setdefault('result',{})[output]=len(original[field])
            if command=='capabilities':
                # Do not advertise the CLI's broader writes as model permissions.
                data={k:v for k,v in data.items() if k not in {'commands','write_commands'}}
                data.update(model_tool_count=len(self.tools),readonly_commands=sorted(set(READ_MAP.values())),
                            model_write_tools=0,model_write_preparations=self.write_preparation_count,human_confirmation='exact_same_chat_code_or_hub_ui',write_preparation_commands=sorted(WRITE_COMMANDS))
            def redact(v):
                if isinstance(v,dict):return {k:redact(x) for k,x in v.items()}
                if isinstance(v,list):return [redact(x) for x in v[:100]]
                return self.clean(v)
            result.update(ok=True,data=redact(data))
        except HubError as e:result.update(error=str(e) if re.fullmatch(r'[a-z][a-z0-9_]{0,95}',str(e)) else 'cli_failed',message='Hub 操作未确认成功，不代表数据为零。')
        except (ValueError,TypeError,KeyError,OSError):result.update(error='cli_invalid_response',message='CLI响应未确认，不代表数据为零。')
        return result

    def tools_for(self,domains):return select_tools(domains)

    def prepare_action(self,name,arguments,actor):
        if actor!=self.actor or not self.message_id or not self.chat_id:raise HubError('trusted_context_required')
        args=dict(validate_action(name,arguments));cmd=ACTIONS[name]['command'];preview={}
        if self.prepared:raise HubError('one_action_per_turn')
        if cmd in REVIEW:
            raw,_=self.invoke(REVIEW[cmd],{'task_id':args['task_id']},actor)
            if not raw.get('ok'):raise HubError(raw.get('error',{}).get('code','review_failed'))
            preview=raw['data']['snapshot'];args.update(review_fingerprint=raw['data']['review_fingerprint'],acknowledge_external_writes=True)
        elif cmd=='wecom.moments-draft':
            raw,_=self.invoke('wecom.moments-scope',{'task_id':args.pop('scope_task_id')},actor)
            if not raw.get('ok'):raise HubError('scope_lookup_failed')
            scope=raw['data'];args.update({k:scope[k] for k in ['binding_id','customer_tag_ids','customer_scope_note']})
        elif cmd=='wecom.moments-refresh':args['acknowledge_platform_read']=True
        # Render/size-check before persisting so a hidden approval cannot be confirmed.
        view=preview or args
        if len(json.dumps(view,ensure_ascii=False,indent=2))>2700:raise HubError('approval_preview_too_large_use_hub')
        row=self.actions.prepare(actor,self.chat_id,self.message_id,cmd,args,view);self.prepared=[row]
        return {'ok':True,'tool':name,'source':self.label,'fetched_at':datetime.now(timezone.utc).isoformat(),
                'data':{'action_id':row['id'],'operation':cmd,'state':row['state'],'execution_triggered':False,'requires_exact_user_confirmation':True}}

    def confirmation_reply(self):
        return self.actions.render(self.prepared[0]) if self.prepared else ''

    def handle_confirmation(self,text,actor,chat_id):
        if actor not in self.allowed_senders:return True,'未授权，未执行。'
        plain=text.strip().lstrip('/').strip()
        if plain=='取消操作':self.actions.cancel(actor,chat_id);return True,'待确认操作已取消，没有提交。'
        lookup=re.fullmatch(r'核对操作\s+([A-Fa-f0-9]{8})',plain)
        if lookup:
            try:
                item=self.actions.inspect(actor,chat_id,lookup[1].upper())
                return True,'本机确认单状态：'+item['state']+'；操作：'+item['command']+'；稳定核对编号：bot-action-'+item['id']+'。未知结果请在Hub核对，不自动重试。'
            except HubError:return True,'未找到本私聊的确认单。'
        m=re.fullmatch(r'确认\s+([A-Fa-f0-9]{8})',plain)
        if not m:
            if plain.startswith('确认') or plain in {'好的','好','继续','同意','执行'} and self.actions.pending(actor,chat_id):
                return True,'尚未执行。请核对上一条完整确认单，再发送“确认 8位确认码”；取消请发送“取消操作”。'
            return False,''
        row=None
        try:
            row=self.actions.claim(actor,chat_id,m[1].upper());args=json.loads(row['args']);cmd=row['command']
            if cmd in REVIEW:
                raw,_=self.invoke(REVIEW[cmd],{'task_id':args['task_id']},actor)
                if not raw.get('ok') or raw.get('data',{}).get('review_fingerprint')!=args['review_fingerprint']:
                    self.actions.finish(row['id'],{'error':'stale_confirmation'},'stale');return True,'内容/版本/状态已变化，旧确认单失效，未执行。请重新准备。'
            args['request_key']='bot-action-'+row['id']
            self._confirming=True
            try:raw,_=self.invoke(cmd,args,actor,row['id'])
            finally:self._confirming=False
            if not raw.get('ok'):
                code=raw.get('error',{}).get('code','cli_failed')
                # Any uncertain write result is terminal locally. Never regenerate/retry.
                state='unknown' if code in {'submission_unknown','cli_timeout','cli_invalid_response','internal_error'} else 'failed'
                self.actions.finish(row['id'],{'error':code},state)
                return True,'操作未确认成功（'+str(code)+'）。请在Hub核对原任务；未自动重试。核对编号：bot-action-'+row['id']
            data=raw['data'];safe=safe_metadata(data);self.last_receipt={'operation':cmd,'data':safe};self.actions.finish(row['id'],safe,'done')
            if cmd=='publish.approve':return True,'已批准该草稿进入Hub发布队列，不等于已发布。请继续查询发布任务状态。\n'+json.dumps(safe,ensure_ascii=False)
            if cmd=='wecom.moments-dispatch':return True,'企微派发已返回回执；仅为成员发表任务，需成员在手机确认，不能当作客户已看到。\n'+json.dumps(safe,ensure_ascii=False)
            return True,'受控操作已返回Hub回执；草稿/队列不等于外部已发布或已送达。\n'+json.dumps(safe,ensure_ascii=False)
        except HubError as exc:return True,'未执行或未重复执行（'+str(exc)+'）。需要新操作时请重新核对Hub，不能重用旧确认码。'
        except Exception:
            if row:self.actions.finish(row['id'],{'error':'submission_unknown'},'unknown')
            return True,'操作结果待核对，未自动重试；请在Hub检查原任务。'

    def handle_command(self,text,actor,message_id):
        if actor not in self.allowed_senders:return True,'未授权，未调用 CLI 或模型。'
        text=text.strip();plain=text.lstrip('/').strip()
        command=None;args={};lead_id=None
        try:
            if plain in {'CLI帮助','命令','cli help'}:return True,HELP
            if plain=='账号':command='accounts.list';args={'platform':'douyin'}
            elif plain=='素材':command='assets.list'
            elif plain=='意向列表':command='leads.list'
            elif plain.startswith('草稿'):
                raw=plain[2:].strip()
                if len(raw.encode())>32000:raise HubError('draft_input_too_large')
                args=json.loads(raw)
                allowed={'account_id','media_type','asset_ids','title','desc','topics','visibility','allow_save','scheduled_at'}
                if (type(args) is not dict or set(args)-allowed or not args.get('asset_ids')
                        or not {'account_id','media_type'}<=set(args)):
                    raise HubError('draft_requires_registered_asset_ids')
                command='publish.draft'
            elif re.fullmatch(r'核对\s+bot-(sync|publish|leads)-[a-f0-9]{48}',plain):
                key=plain.split()[1];command='requests.status';args={'operation':key.split('-')[1],'request_key':key}
            else:
                patterns=[(r'(?:同步作品|同步账号)\s*(\d+)\s*(?:的?作品)?','sync.request','works'),
                    (r'同步评论\s+(\d+)\s+(\d+)','sync.request','comments'),
                    (r'同步状态\s+(\d+)','sync.status','task_id'),(r'发布状态\s+(\d+)','publish.status','task_id'),
                    (r'作品\s+(\d+)','works.list','account_id'),(r'意向分析\s+(\d+)\s+(\d+)','leads.propose','scan'),
                    (r'意向状态\s+(\d+)','leads.status','lead_id'),(r'CRM预览\s+(\d+)','crm.preview','lead_id'),
                    (r'CRM演练\s+(\d+)','crm.mock','lead_id')]
                for pat,cmd,kind in patterns:
                    m=re.fullmatch(pat,plain,flags=re.IGNORECASE)
                    if not m:continue
                    command=cmd;nums=[int(x) for x in m.groups()]
                    if any(n<1 or n>2**31-1 for n in nums):raise HubError('invalid_id')
                    if kind in {'works','comments'}:
                        args={'account_id':nums[0],'kind':kind}
                        if kind=='comments':args['work_id']=nums[1]
                    elif kind=='scan':args={'account_id':nums[0],'work_id':nums[1]}
                    else:args={kind:nums[0]}
                    break
            if command is None:
                if text.startswith('/') or plain.startswith(('草稿','同步作品','同步评论','意向分析','CRM演练','核对')):
                    return True,'命令格式不正确。发送 /CLI帮助；未执行任何业务提交。'
                return False,''
            if command=='crm.mock':
                lead_id=args['lead_id'];meta,_=self.invoke('leads.status',args,actor)
                if not meta.get('ok'):raise HubError('lead_lookup_failed')
                row=meta['data']
                if row.get('state') not in {'approved','recorded_mock'}:raise HubError('human_review_required_in_hub')
                args.update(version=row['version'],digest=row['digest'])
            raw,key=self.invoke(command,args,actor,message_id)
            if not raw.get('ok'):
                code=raw.get('error',{}).get('code','cli_failed')
                if code=='submission_unknown':
                    return True,('提交结果待核对，可能已入库，不能换编号盲重提。\n'+
                        ('发送 /意向状态 '+str(lead_id) if command=='crm.mock' else '发送 /核对 '+str(key)))
                return True,'未确认执行成功（'+str(code)+'）。请在原 Hub 核对；不自动重试或解除风控。'
            d=raw['data'];self.last_receipt={'operation':command,'data':safe_metadata(d)}
            if command=='publish.draft':
                return True,f"草稿 #{d.get('id')} 已保存，未发布。"+('这是禁止真实发布的演示草稿。' if d.get('demo_only') else '请在原 Hub「发布→发布记录→查看并确认」人工核对。')+'\n核对编号：'+str(key)
            if command=='sync.request':return True,f"同步请求 #{d.get('id')} 已入 Hub 队列，当前 {d.get('status')}；等待账号间隔，不会连续补抓。\n查询：/同步状态 {d.get('id')}\n核对编号：{key}"
            if command=='leads.propose':return True,f"本机缓存分析完成：读取 {d.get('scanned')} 条，命中 {d.get('matched')} 条咨询候选，新建 {d.get('created')} 条。不是已核实客户，没有抓取平台或真实入表。\n原 Hub「本账号管理→意向候选」人工复核。"
            if command=='crm.mock':return True,f"本机 CRM 演练完成：模拟记录 #{d.get('mock_record_id')}，去重={d.get('deduplicated')}。未写真实飞书表、未通知真实负责人。"
            if command=='assets.list':return True,'已登记素材（不包含路径）：\n'+'\n'.join(f"{self.clean(x.get('label',''))} | {x.get('id')} | {'演示不可发布' if x.get('is_demo') else '正式素材'}" for x in d.get('items',[])[:20])
            if command in {'accounts.list','works.list','leads.list'}:
                fields=['id','nickname','status'] if command=='accounts.list' else ['id','comment_count','fetched_at'] if command=='works.list' else ['id','state','account_id','work_id']
                rows=[{k:x[k] for k in fields if k in x} for x in d.get('items',[])[:20]]
                return True,'Hub 已存快照（不触发抓取）：\n'+json.dumps(rows,ensure_ascii=False)
            fields=['id','status','state','attempts','fetched','added','next_allowed_at','blocked_reason','demo_only','mode','real_table_configured','receipt_found']
            return True,'Hub 状态：\n'+json.dumps({k:d[k] for k in fields if k in d},ensure_ascii=False)+'\n完整内容在原本机 Hub 核对。'
        except (HubError,ValueError,TypeError,KeyError):
            return True,'命令未完成：参数不正确，或需要先在 Hub 人工复核。发送 /CLI帮助。没有批准发布或真实客户入表。'
