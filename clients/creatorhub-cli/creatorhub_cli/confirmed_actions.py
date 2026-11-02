"""Fixed owner-confirmed actions. JSONL/model bridge still denies these writes.

Review snapshots are for the local human confirmation renderer, NOT for cloud
model context. Server-side digest/version/media checks remain authoritative.
"""
import hashlib,json,re
from .client import field,REQUEST_KEY,require
ID=field('integer',minimum=1)
MID=field('string',pattern=r'^wecom_moment_[a-f0-9]{32}$')
HASH=field('string',pattern=r'^[a-f0-9]{64}$')
CATALOG={
 'wecom.moments-scope':({'task_id':MID},['task_id']),
 'publish.review':({'task_id':ID},['task_id']),
 'publish.approve':({'task_id':ID,'review_fingerprint':HASH,'request_key':REQUEST_KEY,
                    'acknowledge_external_writes':field('boolean')},['task_id','review_fingerprint','request_key','acknowledge_external_writes']),
 'wecom.moments-review':({'task_id':MID},['task_id']),
 'wecom.moments-dispatch':({'task_id':MID,'review_fingerprint':HASH,'request_key':REQUEST_KEY,
                    'acknowledge_external_writes':field('boolean')},['task_id','review_fingerprint','request_key','acknowledge_external_writes'])}
WRITES={'publish.approve','wecom.moments-dispatch'}

def pick(data,keys):
 return {k:data[k] for k in keys if k in data}

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def snapshot(client,command,task_id):
 moments=command.startswith('wecom.')
 raw=client._get(('/api/integrations/wecom/moments/' if moments else '/api/publish/')+str(task_id))
 require(type(raw) is dict and raw.get('id')==task_id,'invalid_response','任务回执不正确',5)
 keys=(['id','label','digest','binding_id','status','demo_only','media_type','text','customer_tag_ids','customer_scope_note','media','approved_at'] if moments else
       ['id','account_id','platform','status','review_version','review_digest','demo_only','review_required','media_type','title','desc','topics','location','visibility','allow_save','scheduled_at','media'])
 data=pick(raw,keys)
 if isinstance(data.get('media'),list):data['media']=[pick(x,['sha256','size','media_type','format']) for x in data['media']]
 require(not data.get('demo_only') and data.get('status') in ({'draft','approved'} if moments else {'draft'}),'not_publishable','仅允许未执行的正式草稿',3)
 require(bool(data.get('digest') if moments else data.get('review_digest')),'invalid_response','缺少审核指纹',5)
 return data

def call(client,command,args):
 if command=='wecom.moments-scope':
  raw=client._get('/api/integrations/wecom/moments/'+args['task_id'])
  require(type(raw) is dict and raw.get('id')==args['task_id'] and raw.get('customer_tag_ids'),'invalid_scope','范围来源任务无效',3)
  return pick(raw,['binding_id','customer_tag_ids','customer_scope_note'])
 data=snapshot(client,command,args['task_id']);fp=fingerprint(data)
 if command not in WRITES:return {'snapshot':data,'review_fingerprint':fp,'external_write_triggered':False}
 require(args['acknowledge_external_writes'] is True,'confirmation_required','需要明确确认外发',3)
 require(args['review_fingerprint']==fp,'stale_confirmation','内容或状态已变化，请重新生成确认单',3)
 tid=args['task_id'];key=args['request_key']
 if command=='publish.approve':
  raw=client._post('/api/publish/'+str(tid)+'/approve',{'review_version':data['review_version'],'review_digest':data['review_digest']},key)
  require(type(raw) is dict and raw.get('id')==tid and raw.get('status')=='pending','submission_unknown','发布入队结果未知，核对原任务，不重新提交',5)
  return {**pick(raw,['id','account_id','status','visibility','approved_at','review_version']),'external_write_triggered':True,'execution':'hub_publish_queue_not_yet_published'}
 root='/api/integrations/wecom/moments/'+tid
 if data['status']=='draft':
  reviewed=client._post(root+'/review',{'digest':data['digest'],'review_note':'Owner confirmed exact Feishu action snapshot',
      'acknowledge_content':True,'acknowledge_customer_tags':True,'acknowledge_member_notification':True},key+'-review')
  require(type(reviewed) is dict and reviewed.get('status')=='approved','submission_unknown','审核结果未知，请核对原任务',5)
 raw=client._post(root+'/submit',{'digest':data['digest'],'acknowledge_external_writes':True},key+'-submit')
 require(type(raw) is dict and raw.get('id')==tid,'submission_unknown','派发结果未知，请核对原任务，不重新创建',5)
 return {**pick(raw,['id','status','phase','job_known','member_published','requires_member_confirmation','last_error_code']),
         'external_write_triggered':True,'execution':'member_task_not_customer_publication'}
