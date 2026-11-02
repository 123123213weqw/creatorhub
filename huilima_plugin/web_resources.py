"""Plugin-owned UI fragments/assets; one explicit host rendering hook."""
import hashlib
from pathlib import Path

WEB_ROOT = Path(__file__).parent / 'web'
SLOTS = ('workspace', 'moments', 'navigation', 'leads', 'assets', 'operations', 'scripts-before', 'scripts-after')
ASSETS = ('operations.js', 'assets-workflows.js', 'wecom.js', 'wecom-moments.js', 'integrations-v9.js')

def asset_path(name):
    if name not in ASSETS:
        raise ValueError('unknown_plugin_asset')
    return WEB_ROOT / name

def render_page(html):
    for slot in SLOTS:
        marker = '<!-- HUILIMA:' + slot + ' -->'
        if html.count(marker) != 1:
            raise RuntimeError('huilima_host_slot_changed:' + slot)
        html = html.replace(marker, (WEB_ROOT / (slot + '.html')).read_text(encoding='utf-8'))
    for name in ASSETS:
        version = hashlib.sha256(asset_path(name).read_bytes()).hexdigest()[:12]
        html = html.replace('/static/' + name + '"', '/static/' + name + '?v=' + version + '"')
    return html
