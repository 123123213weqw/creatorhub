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
