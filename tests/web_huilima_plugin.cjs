const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('huilima_plugin/web/assets-workflows.js', 'utf8');
const context = vm.createContext({calls: []});
vm.runInContext(source, context);
vm.runInContext(`
  refreshLeadCandidates = () => calls.push('leads');
  refreshLocalAssets = () => calls.push('assets');
  refreshOperations = () => calls.push('operations');
`, context);
assert.equal(vm.runInContext('Object.isFrozen(HuilimaPluginUI)', context), true);
for (const name of ['leads', 'assets', 'operations']) {
  assert.equal(vm.runInContext(`HuilimaPluginUI.refreshPanel('${name}')`, context), true);
}
assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(calls)', context)), ['leads', 'assets', 'operations']);
assert.equal(vm.runInContext(`HuilimaPluginUI.refreshPanel('myworks')`, context), false);
assert.equal(vm.runInContext('calls.length', context), 3);
console.log('Huilima plugin panel dispatch: passed (no API calls)');

// Header-level enterprise page: no content-platform enum or account dependency.
vm.runInContext(`
  let selectedPage = 'hub';
  let PLATFORM = 'douyin';
  const buttons = [{dataset:{pf:'douyin'}},{dataset:{pf:'xhs'}},{dataset:{extension:'wecom'}}].map(b => ({...b,
    attributes:{},tabIndex:-1,classList:{toggle(name,value){this[name]=value;}},
    setAttribute(key,value){this.attributes[key]=value;}
  }));
  const document = {
    documentElement:{dataset:{preview:'true'}},
    body:{classList:{toggle(name,value){this[name]=value;}}},
    querySelectorAll(selector){if(selector!=='.pswitch button')throw Error('unexpected selector');return buttons;}
  };
  function switchTab(name,push){selectedPage=name;calls.push('navigate:'+name+':'+push);HuilimaPluginUI.pageChanged(name,PLATFORM);}
  function wcmLoad(){calls.push('local-moments-read');}
`,context);
function evaluate(code){return vm.runInContext(code,context);}
assert.equal(evaluate("HuilimaPluginUI.pageNames.includes('wecom')"),true);
assert.equal(evaluate("HuilimaPluginUI.pageMeta('wecom').platform"),'企业微信');
assert.equal(evaluate("HuilimaPluginUI.pageMeta('hub')"),undefined);
evaluate("HuilimaPluginUI.openWorkspace('wecom')");
assert.equal(evaluate('selectedPage'),'wecom');
assert.equal(evaluate('PLATFORM'),'douyin','enterprise page must not change backend content-platform enum');
assert.equal(evaluate("buttons[2].attributes['aria-selected']"),'true');
assert.equal(evaluate("buttons[0].attributes['aria-selected']"),'false','Douyin is not selected on enterprise page');
assert.equal(evaluate('buttons[2].tabIndex'),0);
assert.equal(evaluate("document.body.classList['huilima-wecom-active']"),true);
assert.equal(evaluate("HuilimaPluginUI.refreshPage('wecom')"),true);
assert.equal(evaluate("calls.includes('local-moments-read')"),false);
evaluate("document.documentElement.dataset.preview='false';HuilimaPluginUI.refreshPage('wecom')");
assert.equal(evaluate("calls.filter(x=>x==='local-moments-read').length"),1);
assert.equal(evaluate("HuilimaPluginUI.refreshPage('hub')"),false);
evaluate("HuilimaPluginUI.pageChanged('hub','xhs')");
assert.equal(evaluate("document.body.classList['huilima-wecom-active']"),false);
assert.equal(evaluate("buttons[1].attributes['aria-selected']"),'true');
assert.equal(evaluate("buttons[2].attributes['aria-selected']"),'false');
assert.deepEqual(JSON.parse(evaluate("JSON.stringify(HuilimaPluginUI.migrateLegacyHubPanel('moments'))")),{page:'wecom',hubPanel:'myworks'});
assert.equal(evaluate("HuilimaPluginUI.migrateLegacyHubPanel('myworks')"),undefined);
evaluate("HuilimaPluginUI.openWorkspace('unknown')");
assert.equal(evaluate('selectedPage'),'wecom');
console.log('Top-level WeCom page, separate selection and legacy migration: passed');
