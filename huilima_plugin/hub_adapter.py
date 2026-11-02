"""The single explicit import boundary to the CreatorHub host.

This adapter does not patch host functions, create another DB, copy account
sessions, execute network requests, or elevate a scoped credential.
"""
from importlib import import_module

HOST_SYMBOLS = {
    "AccountWork": "models",
    "AppSetting": "models",
    "CommentRecord": "models",
    "CommentRule": "models",
    "ContentRecord": "models",
    "DmAutoReplyRule": "models",
    "DouyinAccount": "models",
    "LeadCandidate": "models",
    "LocalMediaAsset": "models",
    "MockCustomerRecord": "models",
    "PublishPacing": "models",
    "PublishTask": "models",
    "RiskAdminAudit": "models",
    "ShareDownloadRecord": "models",
    "SyncPacing": "models",
    "SyncRequest": "models",
    "TaskSubmission": "models",
    "UPLOAD_DIR": "main",
    "VideoBackupConfig": "models",
    "VideoBackupOperation": "models",
    "_identity": "submissions",
    "_require_risk_admin": "main",
    "get_session": "db",
    "get_setting": "settings",
    "local_request": "local_access",
    "replay_if_exists": "submissions",
    "submit_once": "submissions",
    "utc_iso": "scheduling"
}

def main_module():
    return import_module("app.main")

def __getattr__(name):
    module = HOST_SYMBOLS.get(name)
    if module is None:
        raise AttributeError(name)
    return getattr(import_module("app." + module), name)


def compatibility_report(models_module=None):
    """Read declarations only; never open the business DB or touch accounts."""
    models = models_module if models_module is not None else import_module('app.models')
    required = ('LocalMediaAsset', 'LeadCandidate', 'MockCustomerRecord', 'SyncRequest',
                'SyncPacing', 'VideoBackupConfig', 'VideoBackupOperation', 'PublishTask')
    missing = ['models.' + name for name in required if not hasattr(models, name)]
    fields = getattr(getattr(models, 'PublishTask', None), 'model_fields', {})
    for name in ('review_required', 'review_version', 'review_digest', 'media_manifest', 'demo_only'):
        if name not in fields:
            missing.append('PublishTask.' + name)
    return {'host_adapter': 'staged-v1', 'supported': not missing, 'missing_contracts': missing,
            'core_patch_free': False, 'database_opened': False, 'external_io_triggered': False}


def require_compatible_models():
    report = compatibility_report()
    if not report['supported']:
        raise RuntimeError('huilima_host_adapter_incompatible:' + ','.join(report['missing_contracts']))
    return report
