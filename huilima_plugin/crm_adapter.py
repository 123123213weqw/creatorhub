"""CRM field contract + opt-in first-party adapter; no runtime credentials here.

Official contract: https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/create
The live Hub exposes only local_mock, until a real table and its field types are
provided and explicitly enabled. A timeout is NOT interpreted as rejection.
"""
import re
import uuid
from datetime import datetime, timezone
import httpx

DEFAULT_MAPPING = {
    "external_key": "来源唯一编号", "platform": "来源平台", "account_id": "账号编号",
    "work_id": "作品编号", "comment_id": "评论编号", "source_nickname": "来源昵称",
    "comment_text": "咨询原文", "owner_label": "负责人标签", "reviewed_at": "复核时间",
}


class CRMError(Exception):
    pass


FIELD_TYPES = {'text': 1, 'datetime': 5, 'person': 11}


def validate_contract(mapping=None, field_types=None):
    mapping = DEFAULT_MAPPING if mapping is None else mapping
    if (type(mapping) is not dict or set(mapping) != set(DEFAULT_MAPPING)
            or not all(isinstance(v,str) and 1 <= len(v) <= 80 for v in mapping.values())
            or len(set(mapping.values())) != len(mapping)):
        raise CRMError("field_mapping_invalid")
    field_types = {} if field_types is None else field_types
    if type(field_types) is not dict or set(field_types)-set(DEFAULT_MAPPING):
        raise CRMError('field_types_invalid')
    result = {k: field_types.get(k, 'text') for k in DEFAULT_MAPPING}
    for key, kind in result.items():
        allowed = {'text', 'person'} if key == 'owner_label' else {'text', 'datetime'} if key == 'reviewed_at' else {'text'}
        if not isinstance(kind, str) or kind not in allowed:
            raise CRMError('field_types_invalid')
    return mapping, result


def map_fields(payload, mapping=None, *, field_types=None, owner_open_id=None):
    mapping, types = validate_contract(mapping, field_types)
    if set(payload) != set(DEFAULT_MAPPING):
        raise CRMError("payload_contract_invalid")
    result = {}
    for key, name in mapping.items():
        if types[key] == 'person':
            # Identity comes only from the explicitly bound app-scoped owner,
            # never from a label, model output, customer text or another app.
            if not isinstance(owner_open_id, str) or not re.fullmatch(r'ou_[A-Za-z0-9_-]{5,100}', owner_open_id):
                raise CRMError('person_owner_binding_required')
            result[name] = [{'id': owner_open_id}]
        elif types[key] == 'datetime':
            try:
                if not isinstance(payload[key], str):raise ValueError()
                value = datetime.fromisoformat(payload[key].replace('Z', '+00:00'))
                if value.tzinfo is None or value.utcoffset() is None:raise ValueError()
                utc = value.astimezone(timezone.utc)
                if not 1970 <= utc.year <= 2100:raise ValueError()
                delta = utc - datetime(1970, 1, 1, tzinfo=timezone.utc)
                result[name] = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
            except (ValueError, TypeError, OverflowError):
                raise CRMError('reviewed_at_timezone_required') from None
        else:
            result[name] = str(payload[key])
    return result


class FeishuBitableAdapter:
    """Testable transport, deliberately NOT enabled by the Hub/bot config."""
    def __init__(self, app_token, table_id, token_provider, *, enabled=False, http=None):
        if not all(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9]{5,100}',v) for v in (app_token,table_id)):
            raise CRMError("destination_invalid")
        self.url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        self.token_provider, self.enabled = token_provider, enabled
        self.http = http or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)

    def create(self, fields, operation_token):
        if not self.enabled:
            raise CRMError("real_table_not_enabled")
        try:
            parsed = uuid.UUID(operation_token)
            if parsed.version != 4 or str(parsed) != operation_token:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise CRMError("stable_uuid4_required") from None
        if not isinstance(fields,dict) or not fields:raise CRMError("fields_invalid")
        try:
            result = self.http.post(self.url, params={"client_token":operation_token,"ignore_consistency_check":"false","user_id_type":"open_id"},
                headers={"Authorization":"Bearer "+self.token_provider()},json={"fields":fields})
            if result.status_code >= 500:raise CRMError("submission_unknown")
            if result.status_code != 200:raise CRMError("remote_rejected")
            data=result.json()
            if data.get('code') != 0:raise CRMError("remote_rejected")
            rid=data['data']['record']['record_id']
            if not isinstance(rid,str) or not rid:raise ValueError()
            return {"record_id":rid,"mode":"feishu","operation_token":operation_token}
        except (httpx.TransportError,ValueError,KeyError,TypeError):
            raise CRMError("submission_unknown") from None
