"""Domain discovery instead of sending the entire tool catalogue every request."""
import copy
from actions import CATALOG
from readonly_cli import TOOLS as READS
DISCOVER={'type':'function','function':{'name':'hub_tools_discover','description':'按业务加载工具。先选择域和mode，下一轮即可调用具体工具（写操作必须mode=prepare）；不会执行写操作。accounts账号；publish发布/素材；content内容/计划；moments企微朋友圈；distribution员工分发；collection采集；backup备份；operations业务/CRM。',
 'parameters':{'type':'object','properties':{'mode':{'type':'string','enum':['read','prepare'],'description':'纯查询用read（默认）；要准备写操作才用prepare。'},'domain':{'type':'string','enum':['accounts','publish','content','moments','distribution','collection','backup','operations']}},'required':['domain'],'additionalProperties':False}}}
RESOLVE={'type':'function','function':{'name':'hub_resolve_account','description':'仅在本机按用户提供的账号昵称/别名精确解析；只返回唯一账号ID，不向模型返回昵称。没有唯一匹配时必须请用户选ID。',
 'parameters':{'type':'object','properties':{'query':{'type':'string','maxLength':80}},'required':['query'],'additionalProperties':False}}}
TOOLS=READS+[{'type':'function','function':{'name':n,'description':v['description'],'parameters':v['parameters']}} for n,v in CATALOG.items()]+[DISCOVER,RESOLVE]
CORE={'hub_tools_discover','hub_resolve_account','hub_accounts','hub_overview','hub_cli_capabilities'}
DOMAINS={
 'accounts':{'hub_accounts','hub_cli_doctor','hub_cli_works_list'},
 'publish':{'hub_cli_assets_list','hub_cli_publish_list','hub_cli_publish_status'},
 'moments':{'hub_cli_wecom_status','hub_cli_wecom_business_status','hub_cli_wecom_moments_list','hub_cli_wecom_moments_status','hub_cli_wecom_moments_binding'},
 'content':{'hub_cli_content_list','hub_cli_plan_list'},
 'distribution':{'hub_cli_distribution_list','hub_cli_distribution_show','hub_cli_team_list'},
 'collection':{'hub_tasks','hub_collection_results','hub_cli_works_list'},
 'backup':{'hub_cli_backup_list','hub_cli_backup_status','hub_cli_backup_show','hub_cli_backup_policy'},
 'operations':{'hub_cli_operations_readiness','hub_cli_operations_report','hub_cli_operations_handoff','hub_cli_crm_preflight_status'}}
for name,s in CATALOG.items():
 c=s['command']
 d='moments' if c.startswith('wecom.') else 'backup' if c.startswith('backup.') else 'collection' if c.startswith(('sync.','leads.')) else 'distribution' if c.startswith(('distribution.','team.')) else 'content' if c.startswith(('content.','plan.')) else 'publish'
 DOMAINS[d].add(name)

def select(domains):
 names=set(CORE)
 for entry in domains:
  d,_,mode=entry.partition(':');selected=DOMAINS.get(d,set())
  if mode!='prepare':
   selected={n for n in selected if not n.startswith('hub_prepare_') and n!='hub_cli_wecom_moments_binding'}
  names.update(selected)
 return [copy.deepcopy(t) for t in TOOLS if t['function']['name'] in names]
