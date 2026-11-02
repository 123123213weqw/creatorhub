"""Explicit, one-shot customer-ID permission probe; never imported by the Hub.

Official contracts: /document/path/92571 and /document/path/92113.
Only one directly visible test member is allowed. No details, chats or writes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from app.wecom_readonly import (
    API, DEFAULT_CREDENTIALS, MAX_RESPONSE, WeComError, WeComReadOnly,
    load_credentials,
)


class WeComCustomerReadOnly(WeComReadOnly):
    def _external_get(self, route, params):
        # Keep the original setup probe's route allowlist unchanged.
        if route not in {"/externalcontact/get_follow_user_list", "/externalcontact/list"}:
            raise WeComError("wecom_route_rejected")
        for attempt in range(2):
            try:
                with self._http.stream("GET", API + route, params=params) as response:
                    if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                        self._sleep(0.5)
                        continue
                    if response.status_code != 200:
                        raise WeComError("wecom_http_rejected")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE:
                            raise WeComError("wecom_response_too_large")
                    data = json.loads(raw)
                if not isinstance(data, dict) or type(data.get("errcode")) is not int:
                    raise WeComError("wecom_response_invalid")
                if data["errcode"]:
                    raise WeComError("wecom_api_rejected", data["errcode"])
                return data
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 0:
                    self._sleep(0.5)
                    continue
                raise WeComError("wecom_network_unavailable") from None
            except (httpx.HTTPError, ValueError):
                raise WeComError("wecom_response_invalid") from None

    def _authenticated(self, route, *, member=None):
        for attempt in range(2):
            params = {"access_token": self._access_token()}
            if route == "/agent/get":
                params["agentid"] = self.credentials.agent_id
            elif route == "/externalcontact/list":
                if not isinstance(member, str) or not member:
                    raise WeComError("wecom_test_member_required")
                params["userid"] = member
            try:
                request = self._get if route == "/agent/get" else self._external_get
                return request(route, params)
            except WeComError as error:
                if attempt == 0 and error.api_code in {40014, 42001}:
                    self._token, self._expires = "", 0.0
                    continue
                raise

    @staticmethod
    def _ids(value):
        if (not isinstance(value, list) or len(value) > 10000
                or any(not isinstance(x, str) or not x or len(x) > 512
                       or any(ord(c) < 32 or ord(c) == 127 for c in x) for x in value)
                or len(set(value)) != len(value)):
            raise WeComError("wecom_response_invalid")
        return value

    def probe(self, *, acknowledge_customer_ids=False):
        if acknowledge_customer_ids is not True:
            raise WeComError("wecom_customer_id_read_approval_required")
        app = self._authenticated("/agent/get")
        scopes = [app.get(key, {}) for key in ("allow_userinfos", "allow_partys", "allow_tags")]
        if (type(app.get("agentid")) is not int
                or app["agentid"] != self.credentials.agent_id
                or type(app.get("close")) is not int or app["close"] != 0
                or not all(isinstance(scope, dict) for scope in scopes)):
            raise WeComError("wecom_application_mismatch")
        users = scopes[0].get("user", [])
        if (not isinstance(users, list) or len(users) != 1
                or not isinstance(users[0], dict)
                or scopes[1].get("partyid", []) != [] or scopes[2].get("tagid", []) != []):
            raise WeComError("wecom_single_test_member_scope_required")
        member = users[0].get("userid")
        self._ids([member])
        follow = self._authenticated("/externalcontact/get_follow_user_list")
        enabled = self._ids(follow.get("follow_user"))
        if set(enabled) - {member}:
            raise WeComError("wecom_customer_member_scope_changed")
        report = {
            "probe": "single_test_member_customer_ids_only",
            "application_enabled": True, "visible_member_count": 1,
            "enabled_member_count": len(enabled), "customer_members_api_verified": True,
            "customer_id_list_requested": False, "customer_list_api_verified": False,
            "customer_ids_returned": None, "customer_details_read": False,
            "chat_content_read": False, "messages_sent": 0,
            "distribution_tasks_created": 0, "moments_tasks_created": 0,
            "raw_customer_ids_saved": False,
        }
        if not enabled:
            return {**report, "status": "test_member_not_customer_contact_enabled"}
        try:
            customers = self._authenticated("/externalcontact/list", member=member)
        except WeComError as error:
            if error.api_code != 84061:
                raise
            # No relationship is not an accepted empty list, nor a passed sample.
            return {**report, "status": "no_external_contact_relationship",
                    "customer_id_list_requested": True, "customer_list_api_code": 84061}
        ids = self._ids(customers.get("external_userid"))
        return {**report, "status": "ok", "customer_id_list_requested": True,
                "customer_list_api_verified": True, "customer_list_api_code": 0,
                "customer_ids_returned": len(ids)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--ack-single-member-customer-ids", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        if not args.ack_single_member_customer_ids:
            raise WeComError("wecom_customer_id_read_approval_required")
        client = WeComCustomerReadOnly(load_credentials(args.credentials))
        report = client.probe(acknowledge_customer_ids=True)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == "ok" else 1
    except WeComError as error:
        print(json.dumps({"status": "not_ready", "code": error.code,
                          "api_code": error.api_code}, ensure_ascii=False))
        return 1
    except (KeyboardInterrupt, EOFError):
        print('{"status":"cancelled"}')
        return 1
    except Exception:
        # Never expose an HTTP exception's URL, query, token or raw response.
        print('{"status":"unfinished","code":"wecom_probe_internal_error"}')
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
