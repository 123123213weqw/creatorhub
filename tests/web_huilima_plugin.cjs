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

// Workspace transitions are local UI state only; no account or platform writes.
vm.runInContext(`
  let selectedPanel = 'myworks';
  const hostWorkspace = {style: {display: ''}};
  const workspaceButtons = ['platform', 'moments'].map(name => ({
    dataset: {huilimaWorkspace: name}, attributes: {}, tabIndex: name === 'platform' ? 0 : -1,
    setAttribute(key, value) { this.attributes[key] = value; },
    focus() { calls.push('focus:' + name); }
  }));
  const document = {
    documentElement: {dataset: {preview: 'true'}},
    getElementById(id) {
      if (id === 'huilima-platform-workspace') return hostWorkspace;
      return workspaceButtons.find(b => id === 'huilima-' + b.dataset.huilimaWorkspace + '-tab');
    },
    querySelectorAll(selector) {
      if (selector === '[data-huilima-workspace]') return workspaceButtons;
      throw Error('unexpected selector');
    }
  };
  function switchHubTab(name) { selectedPanel = name; HuilimaPluginUI.activateHubPanel(name); }
  function wcmLoad() { calls.push('local-moments-read'); }
`, context);
function evaluate(code) { return vm.runInContext(code, context); }
evaluate(`HuilimaPluginUI.activateHubPanel('operations'); HuilimaPluginUI.openWorkspace('moments');`);
assert.equal(evaluate('selectedPanel'), 'moments');
assert.equal(evaluate('hostWorkspace.style.display'), 'none');
assert.equal(evaluate("workspaceButtons[1].attributes['aria-selected']"), 'true');
assert.equal(evaluate('workspaceButtons[1].tabIndex'), 0);
assert.equal(evaluate('workspaceButtons[0].tabIndex'), -1);
assert.equal(evaluate("HuilimaPluginUI.pageContext('hub', 'moments').platform"), '企业微信');
assert.equal(evaluate("HuilimaPluginUI.pageContext('publish', 'moments')"), undefined);
assert.equal(evaluate("HuilimaPluginUI.pageContext('hub', 'myworks')"), undefined);
assert.equal(evaluate("HuilimaPluginUI.refreshPanel('moments')"), true);
assert.equal(evaluate("calls.includes('local-moments-read')"), false, 'static preview does not connect to business APIs');
evaluate(`HuilimaPluginUI.openWorkspace('platform');`);
assert.equal(evaluate('selectedPanel'), 'operations', 'return preserves previous platform subpage');
assert.equal(evaluate('hostWorkspace.style.display'), '');
evaluate(`HuilimaPluginUI.openWorkspace('unknown');`);
assert.equal(evaluate('selectedPanel'), 'operations');
evaluate(`
  HuilimaPluginUI.workspaceKey({key:'End',target:workspaceButtons[0],preventDefault(){calls.push('prevent');}});
`);
assert.equal(evaluate('selectedPanel'), 'moments');
assert.equal(evaluate("calls.at(-1)"), 'focus:moments');
evaluate(`HuilimaPluginUI.workspaceKey({key:'ArrowLeft',target:workspaceButtons[1],preventDefault(){}});`);
assert.equal(evaluate('selectedPanel'), 'operations');
evaluate(`document.documentElement.dataset.preview = 'false'; HuilimaPluginUI.refreshPanel('moments');`);
assert.equal(evaluate("calls.filter(x => x === 'local-moments-read').length"), 1, 'explicit real-page selection reads local cache once');
console.log('Huilima independent enterprise workspace: passed (no platform I/O)');
