"""Fixed operations commands; no role grant, review, enable or remote dispatch."""
from pathlib import Path
import json,re
from .client import field,REQUEST_KEY,Error,require
ID=lambda prefix:field('string',pattern='^'+prefix+r'_[a-f0-9]{32}$')
TEXT=lambda n=1000:field('string',maxLength=n)
WRITE=lambda d,required:(dict(d,request_key=REQUEST_KEY),required+['request_key'])
CATALOG={
 'team.session':({},[]), 'team.list':({},[]),
 'operations.readiness':({},[]),'operations.health':({},[]),'operations.report':({},[]),
 'content.list':({},[]), 'content.create':WRITE({'label':TEXT(120),'owner_actor_id':TEXT(80)},['label']),
 'content.version':WRITE({'content_id':ID('content'),'title':TEXT(120),'description':TEXT(10000),'tags':TEXT(1000),
    'asset_ids':field('array',items=ID('asset'),minItems=1,maxItems=9)},['content_id','title','asset_ids']),
 'content.variant':WRITE({'version_id':ID('version'),'account_id':field('integer',minimum=1),'title':TEXT(20),'description':TEXT(10000),
    'topics':TEXT(1000),'visibility':field('string',enum=['private','friends','public'])},['version_id','account_id','title']),
 'persona.list':({},[]), 'persona.set':WRITE({'account_id':field('integer',minimum=1),'alias':TEXT(80),'persona':TEXT(2000),'owner_actor_id':TEXT(80)},['account_id']),
 'plan.list':({},[]), 'plan.create':WRITE({'label':TEXT(120),'variant_ids':field('array',items=ID('variant'),minItems=1,maxItems=20)},['label','variant_ids']),
 'plan.drafts':WRITE({'plan_id':ID('plan')},['plan_id']),
 'distribution.list':({},[]), 'distribution.show':({'task_id':ID('distribution')},['task_id']),
 'distribution.materials':({'task_id':ID('distribution')},['task_id']),
 'distribution.create':WRITE({'version_id':ID('version'),'employee_id':ID('actor'),'due_at':TEXT(80),'note':TEXT(1000),
    'targets':field('array',items=field('object',properties={'channel':field('string',enum=['manual','wechat','feishu']),'label':TEXT(160)},required=['channel','label'],additionalProperties=False),minItems=1,maxItems=100)},['version_id','employee_id','due_at','targets']),
 'distribution.change':WRITE({'task_id':ID('distribution'),'revision':field('integer',minimum=1),'action':field('string',enum=['extend','reassign','cancel']),
    'due_at':TEXT(80),'employee_id':ID('actor'),'reason':TEXT(1000)},['task_id','revision','action','reason']),
 'target.claim':WRITE({'target_id':ID('target'),'revision':field('integer',minimum=1),'note':TEXT(1000)},['target_id','revision']),
 'proof.submit':WRITE({'target_id':ID('target'),'revision':field('integer',minimum=1),'file_path':TEXT(4096),'note':TEXT(1000)},['target_id','revision','file_path','note']),
 'proof.show':({'proof_id':ID('proof')},['proof_id']),
 'delivery.list':({},[]),
 'delivery.enqueue':WRITE({'lead_id':field('integer',minimum=1),'version':field('integer',minimum=1),'digest':field('string',pattern='^[a-f0-9]{64}$'),'owner_actor_id':ID('actor')},['lead_id','version','digest','owner_actor_id']),
 'manual.list':({},[]),
 'manual.record':WRITE({'variant_id':ID('variant'),'external_url':TEXT(2000),'published_at':TEXT(80),'note':TEXT(1000)},['variant_id','external_url','published_at','note']),
}
WRITE_COMMANDS={n for n,(p,r) in CATALOG.items() if 'request_key' in r}
GET_ROUTES={'team.session':'session','team.list':'actors','operations.readiness':'readiness','operations.health':'health','operations.report':'reports',
 'content.list':'contents','persona.list':'personas','plan.list':'plans','distribution.list':'distribution','delivery.list':'delivery','manual.list':'manual-publishes'}

def scrub(data):
    from .client import clean
    # Operations never return proof bytes, contacts, paths, token hashes,
    # private connector payloads or secret-bearing audit/config structures.
    forbidden={'file_path','token','token_hash','payload_json','operation_token','owner_mapping_json','credential_env'}
    if isinstance(data,dict):return {k:scrub(v) for k,v in data.items() if k not in forbidden}
    if isinstance(data,list):return [scrub(v) for v in data[:1000]]
    return clean(data)

def call(client,command,args):
    from .client import pick
    path=GET_ROUTES.get(command)
    if command=='distribution.show':path='distribution/'+args['task_id']
    if command=='distribution.materials':path='distribution/'+args['task_id']+'/materials'
    if command=='proof.show':path='proofs/'+args['proof_id']
    if path:raw=client._get('/api/operations/'+path)
    else:
        static={'content.create':'contents','plan.create':'plans','distribution.create':'distribution','delivery.enqueue':'delivery/leads','manual.record':'manual-publishes'}
        path=static.get(command)
        bind={'content.version':('content_id','contents','/versions'),'content.variant':('version_id','versions','/variants'),
              'persona.set':('account_id','accounts','/persona'),'plan.drafts':('plan_id','plans','/drafts'),
              'distribution.change':('task_id','distribution','/change'),'target.claim':('target_id','targets','/claim')}
        omit={'request_key'}
        if command in bind:
            key,pre,suf=bind[command];path=pre+'/'+str(args[key])+suf;omit.add(key)
        if command=='proof.submit':
            path='targets/'+args['target_id']+'/proofs'
            require(Path(args['file_path']).is_absolute() and not args['file_path'].startswith('//'),'invalid_media_path','截图必须是显式本机绝对文件路径',3)
            raw=client.transport.multipart(client.url+'/api/operations/'+path,args['file_path'],{'note':args['note'],'revision':str(args['revision'])},args['request_key'])
        else:raw=client._post('/api/operations/'+path,{k:v for k,v in args.items() if k not in omit},args['request_key'])
        require(type(raw) is dict and (raw.get('id') or (command=='persona.set' and raw.get('account_id')) or (command=='delivery.enqueue' and raw.get('record',{}).get('id'))),'submission_unknown','业务提交回执不完整，请使用原编号与输入核对状态',5)
    result=scrub(raw)
    # Full private review text/photos stay in Hub, not forwarded to a model.
    if command=='proof.show':result=pick(result,['id','task_id','target_id','sha256','size','state','duplicate_suspected','version_digest','created_at'])
    return {'result':result,'state_authority':'existing_creatorhub','external_write_triggered':False,'human_approval_command':False}
