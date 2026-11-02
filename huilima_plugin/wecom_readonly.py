"""Opt-in WeCom setup probe. No customers, messages, task creation or callbacks.

Official contracts: /document/path/91039 and /document/path/90227.
This module is deliberately not imported by the Hub server or scheduler.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import getpass
import json
import logging
import os
from pathlib import Path
import re
import stat
import time

import httpx

API = "https://qyapi.weixin.qq.com/cgi-bin"
DEFAULT_CREDENTIALS = Path.home() / ".config/creatorhub-wecom/credentials.json"
MAX_RESPONSE = 128 * 1024


class WeComError(Exception):
    """Only stable error codes, never upstream text or request URLs."""

    def __init__(self, code: str, api_code: int | None = None):
        self.code, self.api_code = code, api_code
        super().__init__(code)


class _QueryRedaction(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        record.msg = re.sub(
            r"(https://qyapi\.weixin\.qq\.com/[^\s?\"']+)\?[^\s\"']+",
            r"\1?[redacted]", message,
        )
        record.args = ()
        return True


if not any(isinstance(f, _QueryRedaction) for f in logging.getLogger("httpx").filters):
    logging.getLogger("httpx").addFilter(_QueryRedaction())


@dataclass(frozen=True)
class Credentials:
    corp_id: str = field(repr=False)
    agent_id: int
    app_secret: str = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.corp_id, str)
                or not re.fullmatch(r"ww[A-Za-z0-9]{8,64}", self.corp_id)
                or type(self.agent_id) is not int or self.agent_id <= 0
                or not isinstance(self.app_secret, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{8,512}", self.app_secret)):
            raise WeComError("wecom_credentials_invalid")


def load_credentials(path=DEFAULT_CREDENTIALS):
    p = Path(path)
    if not p.is_absolute():
        raise WeComError("wecom_credentials_private_file_required")
    try:
        parent = p.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077
                or parent.st_uid != os.getuid()):
            raise WeComError("wecom_credentials_private_file_required")
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.getuid() or info.st_size > 4096):
                raise WeComError("wecom_credentials_private_file_required")
            data = json.loads(stream.read(4097))
        if not isinstance(data, dict) or set(data) != {"corp_id", "agent_id", "app_secret"}:
            raise WeComError("wecom_credentials_invalid")
        return Credentials(**data)
    except (OSError, ValueError, TypeError):
        raise WeComError("wecom_credentials_unavailable") from None


def provision(credentials: Credentials, path=DEFAULT_CREDENTIALS):
    """Save once; never replace another application's existing secret."""
    p = Path(path)
    if not p.is_absolute():
        raise WeComError("wecom_credentials_private_file_required")
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = p.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid()):
            raise WeComError("wecom_credentials_private_file_required")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"corp_id": credentials.corp_id, "agent_id": credentials.agent_id,
                       "app_secret": credentials.app_secret}, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise WeComError("wecom_credentials_save_failed") from None


class WeComReadOnly:
    def __init__(self, credentials: Credentials, *, transport=None, sleep=time.sleep):
        self.credentials = credentials
        self._http = httpx.Client(
            transport=transport, timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False, trust_env=False,
        )
        self._sleep = sleep
        self._token, self._expires = "", 0.0

    def close(self):
        self._token, self._expires = "", 0.0
        self._http.close()

    def _get(self, route, params):
        if route not in {"/gettoken", "/agent/get"}:
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
                if (not isinstance(data, dict) or type(data.get("errcode")) is not int):
                    raise WeComError("wecom_response_invalid")
                if data["errcode"] != 0:
                    raise WeComError("wecom_api_rejected", data["errcode"])
                return data
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 0:
                    self._sleep(0.5)
                    continue
                raise WeComError("wecom_network_unavailable") from None
            except (httpx.HTTPError, ValueError):
                raise WeComError("wecom_response_invalid") from None

    def _access_token(self):
        if self._token and time.monotonic() < self._expires:
            return self._token
        result = self._get("/gettoken", {"corpid": self.credentials.corp_id,
                                         "corpsecret": self.credentials.app_secret})
        token, expires = result.get("access_token"), result.get("expires_in")
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or type(expires) is not int or expires <= 0):
            raise WeComError("wecom_response_invalid")
        self._token = token
        self._expires = time.monotonic() + max(0, min(expires, 7200) - 60)
        return token

    def doctor(self):
        # Read app configuration only, not employee profiles or customer records.
        for attempt in range(2):
            try:
                result = self._get("/agent/get", {
                    "access_token": self._access_token(),
                    "agentid": self.credentials.agent_id,
                })
                break
            except WeComError as error:
                if attempt == 0 and error.api_code in {40014, 42001}:
                    self._token, self._expires = "", 0.0
                    continue
                raise
        scopes = [result.get(key, {}) for key in ("allow_userinfos", "allow_partys", "allow_tags")]
        if not all(isinstance(scope, dict) for scope in scopes):
            raise WeComError("wecom_response_invalid")
        users, departments, tags = [scope.get(key, []) for scope, key in
                                   zip(scopes, ("user", "partyid", "tagid"))]
        if (type(result.get("agentid")) is not int
                or result["agentid"] != self.credentials.agent_id
                or type(result.get("close")) is not int
                or result["close"] not in {0, 1}
                or not all(isinstance(x, list) for x in (users, departments, tags))):
            raise WeComError("wecom_application_mismatch")
        return {
            "status": "ok", "probe": "application_configuration_only",
            "agent_id_matches": True, "application_enabled": result["close"] == 0,
            "visible_member_count": len(users), "visible_department_count": len(departments),
            "visible_tag_count": len(tags),
            "customer_contact_authorization_verified": False,
            "customer_data_read": False, "messages_sent": 0, "distribution_tasks_created": 0,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["provision", "doctor"])
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--corp-id")
    parser.add_argument("--agent-id", type=int)
    args = parser.parse_args(argv)
    client = None
    try:
        if args.command == "provision":
            if not os.isatty(0):
                raise WeComError("wecom_interactive_terminal_required")
            secret = getpass.getpass("企业微信应用 Secret（输入不会回显，不要发到聊天）：")
            provision(Credentials(args.corp_id, args.agent_id, secret), args.credentials)
            result = {"status": "saved_private", "network_requests": 0}
        else:
            client = WeComReadOnly(load_credentials(args.credentials))
            result = client.doctor()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except WeComError as error:
        print(json.dumps({"status": "not_ready", "code": error.code,
                          "api_code": error.api_code}, ensure_ascii=False))
        return 1
    except (KeyboardInterrupt, EOFError):
        print('{"status":"cancelled"}')
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
