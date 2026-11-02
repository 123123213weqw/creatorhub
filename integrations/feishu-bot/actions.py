"""Durable human confirmation intents; no model-provided identity or approval.

One pending action per actor+chat. Claim before IO; crash/timeout stays unknown,
never retries a POST. Confirmation codes never enter model tool results/context.
"""
import hashlib,json,re,secrets,sqlite3,threading,time
from pathlib import Path
from hub import HubError
CATALOG=json.loads((Path(__file__).parent/'action-catalog.json').read_text())
MAP={name:s['command'] for name,s in CATALOG.items()}
WRITE_NAMES={name for name,s in CATALOG.items() if s['write']}
WRITE_COMMANDS={MAP[name] for name in WRITE_NAMES}
REVIEW={'publish.approve':'publish.review','wecom.moments-dispatch':'wecom.moments-review'}

def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def hashed(v):return hashlib.sha256(v.encode()).hexdigest()
def validate(name,args):
 s=CATALOG.get(name)
 if s is None or type(args) is not dict:raise HubError('invalid_arguments')
 def check(v,p):
  t=p['type']
  if t=='object':
   if type(v) is not dict or not set(p.get('required',[]))<=set(v) or set(v)-set(p['properties']):raise HubError('invalid_arguments')
   for k,x in v.items():check(x,p['properties'][k])
  elif t=='array':
   if type(v) is not list or not p.get('minItems',0)<=len(v)<=p.get('maxItems',20):raise HubError('invalid_arguments')
   for x in v:check(x,p['items'])
  elif t=='integer':
   if type(v) is not int or not p.get('minimum',1)<=v<=p.get('maximum',2**31-1):raise HubError('invalid_arguments')
  elif t=='boolean':
   if type(v) is not bool:raise HubError('invalid_arguments')
  elif t=='string':
   if type(v) is not str or len(v)>p.get('maxLength',512) or ('enum' in p and v not in p['enum']) or ('pattern' in p and re.fullmatch(p['pattern'],v) is None):raise HubError('invalid_arguments')
  else:raise HubError('invalid_arguments')
 check(args,s['parameters'])
 if MAP[name]=='publish.draft' and args['media_type']=='video' and len(args['asset_ids'])!=1:raise HubError('invalid_arguments')
 if MAP[name]=='sync.request' and (('work_id' in args)!=(args['kind']=='comments')):raise HubError('invalid_arguments')
 return args

class ActionStore:
 def __init__(self,path=None,clock=time.time):
  self.clock=clock;self.lock=threading.RLock()
  if path:
   p=Path(path)
   if p.is_symlink():raise ValueError('action database cannot be symlink')
   p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  self.db=sqlite3.connect(str(path) if path else ':memory:',timeout=10,check_same_thread=False)
  self.db.row_factory=sqlite3.Row
  self.db.execute('''CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, code TEXT UNIQUE, owner TEXT,
   chat TEXT, message TEXT, fingerprint TEXT, command TEXT,args TEXT,preview TEXT,state TEXT,created REAL,expires REAL,result TEXT)''')
  # A process interruption cannot imply the remote rejected the write.
  self.db.execute("UPDATE actions SET state='unknown' WHERE state='executing'");self.db.commit()
  if path:Path(path).chmod(0o600)
 def expire(self):
  with self.lock,self.db:
   self.db.execute("UPDATE actions SET state='expired',args='',preview='' WHERE state='pending' AND expires<?",(self.clock(),))
 def prepare(self,actor,chat,message,command,args,preview):
  if not all([actor,chat,message]):raise HubError('trusted_context_required')
  self.expire();fp=digest({'command':command,'args':args,'preview':preview})
  with self.lock,self.db:
   old=self.db.execute('SELECT * FROM actions WHERE owner=? AND chat=? AND message=? AND fingerprint=?',(hashed(actor),hashed(chat),hashed(message),fp)).fetchone()
   if old:
    if old['state']!='pending':raise HubError('confirmation_already_terminal')
    return dict(old)
   if self.db.execute("SELECT 1 FROM actions WHERE owner=? AND chat=? AND state IN ('executing','unknown')",(hashed(actor),hashed(chat))).fetchone():raise HubError('previous_action_unknown_check_hub')
   self.db.execute("UPDATE actions SET state='superseded',args='',preview='' WHERE owner=? AND chat=? AND state='pending'",(hashed(actor),hashed(chat)))
   id='action_'+secrets.token_hex(16);code=secrets.token_hex(4).upper();now=self.clock()
   self.db.execute('INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',(id,code,hashed(actor),hashed(chat),hashed(message),fp,command,json.dumps(args,ensure_ascii=False),json.dumps(preview,ensure_ascii=False),'pending',now,now+600,''))
   return dict(self.db.execute('SELECT * FROM actions WHERE id=?',(id,)).fetchone())
 def pending(self,actor,chat):
  self.expire()
  with self.lock:return [dict(r) for r in self.db.execute("SELECT * FROM actions WHERE owner=? AND chat=? AND state='pending'",(hashed(actor),hashed(chat)))]
 def cancel(self,actor,chat):
  with self.lock,self.db:self.db.execute("UPDATE actions SET state='cancelled',args='',preview='' WHERE owner=? AND chat=? AND state='pending'",(hashed(actor),hashed(chat)))
 def claim(self,actor,chat,code):
  self.expire()
  with self.lock,self.db:
   self.db.execute('BEGIN IMMEDIATE')
   row=self.db.execute('SELECT * FROM actions WHERE code=? AND owner=? AND chat=?',(code,hashed(actor),hashed(chat))).fetchone()
   if not row:raise HubError('confirmation_not_found')
   if row['state']!='pending':raise HubError('confirmation_'+row['state'])
   self.db.execute("UPDATE actions SET state='executing' WHERE id=? AND state='pending'",(row['id'],))
   return dict(row)
 def finish(self,id,result,state):
  with self.lock,self.db:
   self.db.execute('UPDATE actions SET state=?,result=?,args=?,preview=? WHERE id=?',(state,json.dumps(result,ensure_ascii=False),
    self.db.execute('SELECT args FROM actions WHERE id=?',(id,)).fetchone()[0] if state=='unknown' else '',
    self.db.execute('SELECT preview FROM actions WHERE id=?',(id,)).fetchone()[0] if state=='unknown' else '',id))
 def inspect(self,actor,chat,code):
  with self.lock:
   row=self.db.execute('SELECT * FROM actions WHERE code=? AND owner=? AND chat=?',(code,hashed(actor),hashed(chat))).fetchone()
   if not row:raise HubError('confirmation_not_found')
   return dict(row)
 def render(self,row):
  preview=json.loads(row['preview']);args=json.loads(row['args'])
  body=json.dumps(preview if preview else args,ensure_ascii=False,indent=2)
  # No silent truncation of an approval preview.
  if len(body)>2700:raise HubError('approval_preview_too_large_use_hub')
  return ('待确认操作：'+row['command']+'\n'+body+'\n'
    '以上内容/账号/范围需逐项核对；可能对平台外发或启动采集。素材内容请同时在本机Hub预览。\n'
    '10分钟内，仅在本私聊发送：确认 '+row['code']+'\n取消请发送：取消操作。单说“好的/继续”不会执行。\n'
    '朋友圈派发仅创建成员任务，仍需成员手机发表；发布入队不等于已发布。')
