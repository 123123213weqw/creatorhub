"""Explicit, idempotent plugin registration; no import-time network or writes."""
import asyncio
from importlib import import_module

from fastapi import APIRouter
from fastapi.responses import FileResponse
from . import __version__
from .web_resources import ASSETS, asset_path, render_page

ROUTER_MODULES = ('operations_workflows', 'video_backup', 'business_workflows',
    'delivery_workflows', 'operations_health', 'wecom_workflows', 'crm_preflight',
    'observations', 'wecom_business_workflows', 'delivery_profile', 'wecom_moments')
RECOVERY_MODULES = ('sync_requests', 'video_backup', 'delivery_workflows', 'wecom_moments')

class PluginRuntime:
    version = __version__

    def recover(self):
        return sum(import_module('huilima_plugin.' + name).recover() for name in RECOVERY_MODULES)

    def start_tasks(self):
        from .operations_health import monitor_loop
        return [asyncio.create_task(monitor_loop(), name='huilima-operations-health')]

    async def stop_tasks(self, tasks):
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def render_page(self, html):
        return render_page(html)

    def health(self):
        from .hub_adapter import get_session
        from .delivery_workflows import config
        with get_session() as session:
            crm_mode = config(session).mode
        return {'hub_cli_protocol': '1.0', 'draft_review': True, 'controlled_sync_queue': True,
            'local_assets': True, 'offline_leads': True, 'crm_mode': crm_mode,
            'video_backup_protocol': '1.0', 'business_operations_protocol': '1.0',
            'team_gateway_enabled': False, 'automatic_external_delivery': False,
            'wecom_readonly_protocol': '1.0', 'wecom_automatic_polling': False,
            'business_integrations_protocol': '1.0', 'typed_crm_fields': True, 'backup_policy_plan_only': True,
            'wecom_moments_protocol': '1.0', 'wecom_moments_automatic_submit': False,
            'wecom_moments_manual_approval': True, 'huilima_plugin_version': self.version,
            'huilima_host_adapter': 'staged-v1'}


def install(app):
    existing = getattr(app.state, 'huilima_plugin', None)
    if existing is not None:
        if existing.version != __version__:
            raise RuntimeError('another_huilima_version_already_registered')
        return existing
    from .hub_adapter import require_compatible_models
    require_compatible_models()
    from .business_workflows import TeamBoundaryMiddleware
    app.add_middleware(TeamBoundaryMiddleware)
    for name in ROUTER_MODULES:
        app.include_router(import_module('huilima_plugin.' + name).router)
    assets = APIRouter()
    def handler(name):
        async def serve():
            return FileResponse(asset_path(name), media_type='text/javascript',
                                headers={'Cache-Control': 'no-cache'})
        return serve
    for name in ASSETS:
        # Exact filenames only; never resolve a request-controlled filesystem path.
        assets.add_api_route('/extensions/huilima/static/' + name, handler(name), methods=['GET','HEAD'], include_in_schema=False)
        assets.add_api_route('/static/' + name, handler(name), methods=['GET','HEAD'], include_in_schema=False)
    @assets.get('/api/plugins/huilima/manifest')
    async def manifest():
        return {'name': 'huilima', 'version': __version__, 'host_adapter': 'staged-v1',
            'migration_stage': 'modules-and-ui-extracted', 'core_patch_free': False,
            'automatic_external_writes': False, 'automatic_schema_migration': False,
            'database_authority': 'existing_creatorhub', 'model_tools': 46,
            'write_preparations': 16, 'direct_model_external_writes': 0}
    app.include_router(assets)
    runtime = PluginRuntime()
    app.state.huilima_plugin = runtime
    return runtime
