import argparse
from datetime import datetime,timezone
import json
import os
import sys
import uuid
from .client import CATALOG,Client,Error,configured_url,require,WRITE_COMMANDS


class Parser(argparse.ArgumentParser):
    def error(self,message):raise Error('cli_usage','命令或参数不符合约定；使用 --help 或 schema 查看用法')


def parser():
    p=Parser(description='现有 CreatorHub 的 JSON CLI；不创建新中控台、数据库、审批或队列。')
    p.add_argument('--config',default=os.environ.get('HUBCTL_CONFIG'))
    p.add_argument('--json',action='store_true')
    p.add_argument('--version',action='version',version='0.12.0')
    sub=p.add_subparsers(dest='root_command',required=True,parser_class=Parser);groups={}
    for command,(props,required) in CATALOG.items():
        parts=command.split('.')
        if len(parts)==1:child=sub.add_parser(command)
        else:
            if parts[0] not in groups:groups[parts[0]]=sub.add_parser(parts[0]).add_subparsers(dest='action',required=True,parser_class=Parser)
            child=groups[parts[0]].add_parser(parts[1])
        child.set_defaults(command=command);child.add_argument('--input');child.add_argument('--json',action='store_true',help=argparse.SUPPRESS)
        for key,spec in props.items():
            kind=spec['type']
            convert=int if kind=='integer' else json.loads if kind in {'array','boolean','object'} else str
            child.add_argument('--'+key.replace('_','-'),type=convert,default=None)
    return p


def main(argv=None):
    envelope={'schema_version':'1.0','request_id':uuid.uuid4().hex,'generated_at':datetime.now(timezone.utc).isoformat()}
    writing=False
    try:
        args=parser().parse_args(argv)
        props=CATALOG[args.command][0]
        values={k:getattr(args,k) for k in props if getattr(args,k) is not None}
        if args.input is not None:
            require(not values,'ambiguous_input','JSON 输入不能与业务参数混用')
            raw=sys.stdin.read(65537) if args.input=='-' else args.input
            require(len(raw.encode('utf-8'))<=65536,'input_too_large','JSON 输入超过 64 KiB')
            values=json.loads(raw)
        client=Client(configured_url(args.config))
        writing=args.command in WRITE_COMMANDS
        result=client.call(args.command,values)
        print(json.dumps({**envelope,'ok':True,'data':result},ensure_ascii=False,allow_nan=False));return 0
    except Error as e:
        print(json.dumps({**envelope,'ok':False,'error':{'code':e.code,'message':e.message}},ensure_ascii=False));return e.exit_code
    except (ValueError,TypeError,KeyError,AttributeError,OSError):
        code='submission_unknown' if writing else 'invalid_input_or_response'
        message='提交可能已入库；沿用原编号和输入核对，勿新建提交' if writing else '输入、配置或 Hub 响应格式不正确'
        print(json.dumps({**envelope,'ok':False,'error':{'code':code,'message':message}},ensure_ascii=False));return 5
    except KeyboardInterrupt:
        print(json.dumps({**envelope,'ok':False,'error':{'code':'interrupted','message':'操作中断；若是提交操作，请沿用原编号核对结果，勿新建提交'}},ensure_ascii=False));return 130
    except Exception:
        print(json.dumps({**envelope,'ok':False,'error':{'code':'internal_error','message':'操作未成功；提交可能已入库，请沿用原编号核对，勿盲目重提'}},ensure_ascii=False));return 5


if __name__=='__main__':raise SystemExit(main())
