"""Narrow official Moments transport. No chats, customer details or auto-send.

Contracts: /document/path/95094, /95098 and /93333. Creating the enterprise
task notifies a member; that member still needs to publish in WeCom. None of
the write POSTs have a documented idempotency token, so NEVER retry them.
"""
from __future__ import annotations

import hashlib
import json

import httpx

from .wecom_business_readonly import WeComBusinessReadOnly, identifier
from .wecom_readonly import API, MAX_RESPONSE, WeComError


class MomentWriteError(WeComError):
    def __init__(self, code, api_code=None, *, uncertain=False):
        super().__init__(code, api_code)
        self.uncertain = uncertain


def binding_digest(credentials, member):
    value = [credentials.corp_id, credentials.agent_id, member]
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()


class WeComMomentsClient(WeComBusinessReadOnly):
    def binding(self):
        member = self._member()
        return {'binding_id': binding_digest(self.credentials, member),
                'sender_user_id': member}

    def assert_binding(self, binding_id, member):
        actual = self.binding()
        if actual != {'binding_id': binding_id, 'sender_user_id': member}:
            raise WeComError('wecom_moment_application_or_member_changed')

    @staticmethod
    def _decode(response):
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('response_limit')
        data = json.loads(raw)
        if type(data) is not dict or type(data.get('errcode')) is not int:
            raise ValueError('response_invalid')
        return data

    def _write_once(self, route, before, *, body=None, files=None, params=None):
        if route not in {'/externalcontact/add_moment_task', '/media/upload_attachment'}:
            raise WeComError('wecom_route_rejected')
        access = self._access_token()  # Auth failures occur BEFORE claiming a POST.
        before()  # Durable claim must commit before the first transmitted byte.
        try:
            with self._http.stream('POST', API + route,
                    params={'access_token': access, **(params or {})},
                    **({'json': body} if body is not None else {'files': files})) as response:
                if response.status_code != 200:
                    raise MomentWriteError('wecom_moment_submission_unknown', uncertain=True)
                data = self._decode(response)
            if data['errcode'] != 0:
                raise MomentWriteError('wecom_moment_api_rejected', data['errcode'])
            return data
        except MomentWriteError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise MomentWriteError('wecom_moment_submission_unknown', uncertain=True) from None

    def upload(self, item, content, before):
        kind = 'video' if item['media_type'] == 'video' else 'image'
        suffix = '.mp4' if kind == 'video' else '.png' if item['format'] == 'PNG' else '.jpg'
        name = 'hub-' + item['sha256'][:16] + suffix
        mime = 'video/mp4' if kind == 'video' else 'image/png' if suffix == '.png' else 'image/jpeg'
        files = {'media': (name, content, mime, {'Content-Disposition':
            f'form-data; name="media"; filename="{name}"; filelength={len(content)}'})}
        result = self._write_once('/media/upload_attachment', before, files=files,
                                  params={'media_type': kind, 'attachment_type': 1})
        try:
            media = identifier(result.get('media_id'))
            if result.get('type') != kind or type(result.get('created_at')) is not int:
                raise ValueError()
            return {'media_id': media, 'created_at': result['created_at'], 'type': kind}
        except (WeComError, ValueError):
            raise MomentWriteError('wecom_moment_media_receipt_unknown', uncertain=True) from None

    def create(self, body, before):
        result = self._write_once('/externalcontact/add_moment_task', before, body=body)
        try:
            job = identifier(result.get('jobid'))
            if len(job.encode('utf-8')) > 64:
                raise ValueError()
            return job
        except (WeComError, ValueError):
            raise MomentWriteError('wecom_moment_submission_unknown', uncertain=True) from None

    def _read_result(self, route, *, body=None, params=None):
        if route not in {'/externalcontact/get_moment_task_result',
                         '/externalcontact/get_moment_task'}:
            raise WeComError('wecom_route_rejected')
        for attempt in range(2):
            try:
                with self._http.stream('POST' if body is not None else 'GET', API + route,
                        params={'access_token': self._access_token(), **(params or {})},
                        **({'json': body} if body is not None else {})) as response:
                    if response.status_code in {429, 500, 502, 503, 504} and not attempt:
                        self._sleep(.5)
                        continue
                    if response.status_code != 200:
                        raise WeComError('wecom_moment_read_http_rejected')
                    data = self._decode(response)
                if data['errcode'] in {40014, 42001} and not attempt:
                    self._token, self._expires = '', 0.
                    continue
                if data['errcode']:
                    raise WeComError('wecom_moment_read_api_rejected', data['errcode'])
                return data
            except (httpx.TimeoutException, httpx.NetworkError):
                if not attempt:
                    self._sleep(.5)
                    continue
                raise WeComError('wecom_moment_read_unavailable') from None
            except (httpx.HTTPError, ValueError, TypeError):
                raise WeComError('wecom_moment_read_response_invalid') from None
        raise WeComError('wecom_moment_read_unavailable')

    def creation_result(self, job):
        data = self._read_result('/externalcontact/get_moment_task_result', params={'jobid': job})
        status = data.get('status')
        if type(status) is not int or status not in {1, 2, 3} or data.get('type') != 'add_moment_task':
            raise WeComError('wecom_moment_read_response_invalid')
        if status != 3:
            return {'status': status}
        result = data.get('result')
        if not isinstance(result, dict) or type(result.get('errcode')) is not int:
            raise WeComError('wecom_moment_read_response_invalid')
        if result['errcode']:
            return {'status': 3, 'api_code': result['errcode'], 'rejected': True}
        invalid = 0
        for key, names in [('invalid_sender_list', ('user_list', 'department_list')),
                           ('invalid_external_contact_list', ('tag_list',))]:
            values = result.get(key, {})
            if not isinstance(values, dict):
                raise WeComError('wecom_moment_read_response_invalid')
            for name in names:
                entries = values.get(name, [])
                if not isinstance(entries, list):
                    raise WeComError('wecom_moment_read_response_invalid')
                invalid += len(entries)
        moment = result.get('moment_id', '')
        if not invalid or moment:
            moment = identifier(moment)
        return {'status': 3, 'moment_id': moment, 'scope_rejected': bool(invalid)}

    def member_result(self, moment, member):
        data = self._read_result('/externalcontact/get_moment_task',
                                 body={'moment_id': moment, 'limit': 100})
        rows = data.get('task_list')
        if (not isinstance(rows, list) or len(rows) > 1
                or data.get('next_cursor', '') != ''):
            raise WeComError('wecom_moment_member_result_scope_invalid')
        if not rows:
            return {'member_task_observed': False, 'member_published': None}
        row = rows[0]
        if (not isinstance(row, dict) or row.get('userid') != member
                or type(row.get('publish_status')) is not int
                or row['publish_status'] not in {0, 1}):
            raise WeComError('wecom_moment_member_result_scope_invalid')
        return {'member_task_observed': True, 'member_published': row['publish_status'] == 1}
