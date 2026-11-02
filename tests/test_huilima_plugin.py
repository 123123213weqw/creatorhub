"""Staged plugin contract; no tenant data, network, startup or migrations."""
import asyncio,ast,importlib,json
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from sqlmodel import SQLModel
from huilima_plugin import __version__
from huilima_plugin.bootstrap import install,PluginRuntime
from huilima_plugin.hub_adapter import compatibility_report
from huilima_plugin.web_resources import ASSETS,SLOTS,asset_path,render_page
from test_access_and_submissions import client
from test_project_optimizations import local_project
ROOT=Path(__file__).resolve().parents[1];PLUGIN=ROOT/'huilima_plugin'


def test_version_and_staged_scope_are_explicit():
 import tomllib
 m=json.loads((PLUGIN/'plugin.json').read_text());p=tomllib.loads((PLUGIN/'pyproject.toml').read_text())
 assert __version__==m['version']==p['project']['version']
 assert not m['core_patch_free'] and not m['automatic_external_writes'] and not m['automatic_schema_migration']
 assert m['database_authority']=='existing_creatorhub'
 assert p['project']['entry-points']['creatorhub.plugins']['huilima']=='huilima_plugin.bootstrap:install'


@pytest.mark.parametrize('name',['wecom_readonly','wecom_workflows','wecom_customer_readonly','wecom_business_readonly','wecom_business_workflows','wecom_moments','wecom_moments_client','wecom_moments_media','business_models','business_workflows','business_backup','cloud_storage_models','crm_adapter','crm_preflight','delivery_profile','delivery_workflows','feishu_drive','observations','operations_health','operations_workflows','publish_review','sync_requests','video_backup'])
def test_legacy_import_is_same_module_not_second_implementation(name):
 old=importlib.import_module('app.'+name);new=importlib.import_module('huilima_plugin.'+name)
 assert old is new and old.__name__=='huilima_plugin.'+name


def test_models_share_the_original_metadata_tables():
 from app.business_models import WeComMomentTask as old
 from huilima_plugin.business_models import WeComMomentTask as new
 assert old is new and old.__table__ is SQLModel.metadata.tables[old.__tablename__]
 assert old.__tablename__=='wecommomenttask'


def test_no_plugin_implementation_imports_host_outside_adapter():
 for f in PLUGIN.glob('*.py'):
  if f.name=='hub_adapter.py':continue
  for n in ast.walk(ast.parse(f.read_text())):
   if isinstance(n,ast.ImportFrom):assert not (n.module or '').startswith('app.'),f.name
   if isinstance(n,ast.Import):assert not any(x.name=='app' or x.name.startswith('app.') for x in n.names),f.name


def test_missing_host_contract_fails_closed_without_db():
 r=compatibility_report(SimpleNamespace())
 assert not r['supported'] and 'PublishTask.review_digest' in r['missing_contracts']
 assert not r['database_opened'] and not r['external_io_triggered']


def test_current_host_contract_supported():assert compatibility_report()['supported']


def test_install_is_idempotent_and_keeps_route_identity(local_project):
 app=FastAPI();first=install(app);count=len(app.routes)
 assert install(app) is first and len(app.routes)==count
 assert len({(r.path,tuple(sorted(r.methods or []))) for r in app.routes})==count


def test_existing_host_has_single_plugin_registration(local_project):
 from app.main import app
 assert app.state.huilima_plugin.version==__version__
 matches=[r for r in app.routes if r.path=='/api/integrations/wecom/moments' and 'GET' in (r.methods or [])]
 assert len(matches)==1 and len([r for r in app.routes if r.path=='/api/plugins/huilima/manifest'])==1


def test_foreign_plugin_version_cannot_be_silently_replaced():
 app=FastAPI();app.state.huilima_plugin=SimpleNamespace(version='incompatible-version')
 with pytest.raises(RuntimeError,match='another_huilima_version'):install(app)


def test_resource_path_is_allowlisted():
 for name in ASSETS:assert asset_path(name).is_file()
 for name in ['../credentials.json','/etc/passwd','config.json','unknown.js']:
  with pytest.raises(ValueError):asset_path(name)


def test_fragments_are_composed_exactly_once():
 shell=(ROOT/'app/web/index.html').read_text()
 assert all(shell.count('<!-- HUILIMA:'+x+' -->')==1 for x in SLOTS)
 full=render_page(shell)
 assert '<!-- HUILIMA:' not in full and full.count('id="wcm-panel"')==1
 assert full.count('data-hubpanel="operations"')==1 and full.count('id="video-backup-status"')==1
 assert full.index('/static/assets-workflows.js')<full.index('/static/app.js')
 assert full.index('/static/wecom-moments.js')>full.index('/static/app.js') and '?v=' in full


def test_changed_host_slot_is_detected_not_silently_injected():
 with pytest.raises(RuntimeError,match='huilima_host_slot_changed'):render_page('<html>unrecognized host</html>')


def test_plugin_api_resources_and_original_routes_are_available(local_project):
 async def run():
  async with client() as http:
   m=await http.get('/api/plugins/huilima/manifest');assert m.status_code==200 and not m.json()['core_patch_free']
   assert (await http.get('/')).text.count('id="wcm-panel"')==1
   for name in ASSETS:
    a=await http.get('/extensions/huilima/static/'+name);b=await http.get('/static/'+name)
    assert a.status_code==b.status_code==200 and a.content==b.content==asset_path(name).read_bytes()
    assert a.headers['x-content-type-options']=='nosniff'
   assert (await http.get('/extensions/huilima/static/config.json')).status_code==404
   assert (await http.get('/api/integrations/wecom/moments')).status_code==200
 asyncio.run(run())


def test_plugin_lifecycle_cancels_only_its_tasks():
 async def run():
  pending=asyncio.create_task(asyncio.sleep(600));unrelated=asyncio.create_task(asyncio.sleep(600))
  await PluginRuntime().stop_tasks([pending]);assert pending.cancelled() and not unrelated.done()
  unrelated.cancel();await asyncio.gather(unrelated,return_exceptions=True)
 asyncio.run(run())


def test_frontend_plugin_refresh_dispatch():
 import shutil,subprocess
 node=shutil.which('node')
 if not node:pytest.skip('Node is unavailable')
 r=subprocess.run([node,'tests/web_huilima_plugin.cjs'],cwd=ROOT,capture_output=True,text=True,timeout=10)
 assert r.returncode==0,r.stderr


def test_moments_workspace_not_nested_in_account_or_operations():
 from html.parser import HTMLParser
 class Structure(HTMLParser):
  def __init__(self):super().__init__();self.stack=[];self.parents={};self.ids=[]
  def handle_starttag(self,tag,attrs):
   attrs=dict(attrs)
   if 'id' in attrs:self.ids.append(attrs['id']);self.parents[attrs['id']]=list(self.stack)
   if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:self.stack.append((tag,attrs))
  def handle_endtag(self,tag):
   for i in range(len(self.stack)-1,-1,-1):
    if self.stack[i][0]==tag:self.stack=self.stack[:i];break
 parser=Structure();parser.feed(render_page((ROOT/'app/web/index.html').read_text()))
 assert len(parser.ids)==len(set(parser.ids))
 ancestors=parser.parents['wcm-panel']
 assert any(a.get('data-panel')=='wecom' for _,a in ancestors)
 assert not any(a.get('data-hubpanel')=='operations' or a.get('id')=='huilima-platform-workspace' for _,a in ancestors)
 assert not any(a.get('data-panel')=='hub' for _,a in ancestors)
 assert (PLUGIN/'web/operations.html').read_text().count('wcm-panel')==0
 top=(PLUGIN/'web/platform-navigation.html').read_text()
 assert 'data-extension="wecom"' in top and 'data-pf="wecom"' not in top
 assert 'aria-controls="huilima-wecom-page"' in top
 assert any(tag=='header' for tag,_ in parser.parents['huilima-wecom-tab'])
 assert any(a.get('class')=='pswitch' for _,a in parser.parents['huilima-wecom-tab'])
 assert 'role="tablist" aria-label="企业微信功能"' in (PLUGIN/'web/moments.html').read_text()


def test_host_keeps_minimal_explicit_plugin_workspace_hooks():
 js=(ROOT/'app/web/app.js').read_text()
 assert 'HuilimaPluginUI?.pageMeta?.(name)' in js
 assert 'HuilimaPluginUI?.pageNames || []' in js
 assert 'buttons[index].click()' in js
 assert 'if (leavingExtensionPage) switchTab("hub", true)' in js
 assert 'HuilimaPluginUI?.pageContext?.(name, HUB_TAB)' in js
 assert 'wecom/moments' not in js


def test_customer_facing_copy_is_plain_but_keeps_essential_limits():
 html=(PLUGIN/'web/moments.html').read_text()
 for label in ['查看草稿和进度','确认发表成员','新建朋友圈','找回未完成操作','不会直接发布','不会默认发给所有客户','10MB','30秒']:
  assert label in html
 for developer_label in ['本地缓存','原请求回执','单次提交','平台写权限仍须真实验收','Huilima 独立工作区']:
  assert developer_label not in html
 js=(PLUGIN/'web/wecom-moments.js').read_text()
 assert "card.append(details('查看处理记录'" in js
 assert '成员已发表（不代表客户已阅读）' in js
 assert '不撤回企业微信中的任务' in js
 assert '由管理员提供' in js
 assert "diagnostic('error'" in js
