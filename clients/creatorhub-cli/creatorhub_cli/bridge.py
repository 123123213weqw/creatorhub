"""Bounded JSONL client, not a network listener or an MCP server.

Use with an AI runtime that can spawn a local executable. No eval, shell, model
API or credential copying. Read-only by default; selected local preparation
commands require an explicit per-launch allowlist.
"""
import argparse,json,re,sys,uuid
from datetime import datetime,timezone
from .client import CATALOG,WRITE_COMMANDS,Client,Error,configured_url,require

PREPARATION={'publish.draft','observations.import','content.create','content.version','content.variant',
             'persona.set','plan.create','plan.drafts','wecom.moments-draft'}


def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate key')
        result[key]=value
    return result


def process(client,raw,allowed=()):
    envelope={'schema_version':'1.0','request_id':uuid.uuid4().hex,
              'generated_at':datetime.now(timezone.utc).isoformat()}
    command=None
    try:
        require(len(raw.encode('utf-8'))<=65536,'input_too_large','每行最多64KiB')
        request=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        require(type(request) is dict and not set(request)-{'id','command','args'},'invalid_request','只允许id、command、args')
        identity=request.get('id')
        require(isinstance(identity,str) and bool(re.fullmatch(r'[A-Za-z0-9._:-]{1,80}',identity)),
                'invalid_request_id','每次调用需提供不含敏感信息的请求编号')
        envelope['id']=identity;command=request.get('command')
        require(isinstance(command,str) and command in CATALOG,'unknown_command','先读取schema获取可用命令')
        require(command not in {'publish.review','wecom.moments-review','wecom.moments-scope'},'command_not_allowed','完整人工审核快照仅供本机确认器，不能直通模型',3)
        require(command not in WRITE_COMMANDS or command in allowed and command in PREPARATION,
                'command_not_allowed','默认只读；不允许平台刷新、外发、审批或隐式启用',3)
        args=request.get('args',{})
        require(type(args) is dict,'invalid_arguments','args必须为对象')
        result=client.call(command,args)
        return {**envelope,'ok':True,'data':result}
    except Error as e:
        return {**envelope,'ok':False,'error':{'code':e.code,'message':e.message}}
    except Exception:
        return {**envelope,'ok':False,'error':{'code':'submission_unknown' if command in WRITE_COMMANDS else 'invalid_input_or_response',
            'message':'未确认成功；提交操作沿用原request_key和相同输入核对，勿重新编号'}}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config');p.add_argument('--allow-command',action='append',default=[],choices=sorted(PREPARATION))
    p.add_argument('--max-requests',type=int,default=200)
    args=p.parse_args(argv)
    if not 1<=args.max_requests<=1000:p.error('max-requests must be 1..1000')
    try:client=Client(configured_url(args.config))
    except Exception:
        print(json.dumps({'ok':False,'error':{'code':'configuration_invalid','message':'本机Hub配置不可用'}}));return 2
    for _ in range(args.max_requests):
        raw=sys.stdin.readline(65538)
        if not raw:break
        if len(raw.encode('utf-8'))>65536:
            # Refuse the stream rather than treating its remainder as another call.
            print(json.dumps({'ok':False,'error':{'code':'input_too_large','message':'每行最多64KiB'}}),flush=True);return 2
        print(json.dumps(process(client,raw,set(args.allow_command)),ensure_ascii=False,allow_nan=False),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
