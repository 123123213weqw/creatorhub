import json
import logging
import os

import httpx
import pytest

from app.wecom_readonly import (
    Credentials, MAX_RESPONSE, WeComError, WeComReadOnly, load_credentials, main, provision,
)

CORP = "wwtest000000000000"
SECRET = "test-secret-not-a-live-credential"
TOKEN = "test-token-not-live"


def credentials():
    return Credentials(CORP, 1000002, SECRET)


def app_result(**values):
    return {"errcode": 0, "agentid": 1000002, "close": 0,
            "allow_userinfos": {"user": [{"userid": "private-member"}]}, **values}


def token_result():
    return {"errcode": 0, "access_token": TOKEN, "expires_in": 7200}


def client(handler):
    return WeComReadOnly(credentials(), transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_private_credential_roundtrip_and_no_overwrite(tmp_path):
    p = tmp_path / "private" / "credentials.json"
    provision(credentials(), p)
    assert p.stat().st_mode & 0o777 == 0o600
    assert p.parent.stat().st_mode & 0o777 == 0o700
    assert load_credentials(p) == credentials()
    with pytest.raises(WeComError, match="save_failed"):
        provision(credentials(), p)
    assert SECRET not in repr(credentials()) and CORP not in repr(credentials())


@pytest.mark.parametrize("mode,target", [(0o644, "file"), (0o755, "directory")])
def test_reject_public_credentials(tmp_path, mode, target):
    p = tmp_path / "private" / "credentials.json"
    provision(credentials(), p)
    os.chmod(p if target == "file" else p.parent, mode)
    with pytest.raises(WeComError, match="private_file_required"):
        load_credentials(p)


def test_reject_symlink(tmp_path):
    p = tmp_path / "private" / "credentials.json"
    provision(credentials(), p)
    linked = p.parent / "linked.json"
    linked.symlink_to(p)
    with pytest.raises(WeComError):
        load_credentials(linked)


def test_reject_extra_fields(tmp_path):
    p = tmp_path / "private" / "credentials.json"
    provision(credentials(), p)
    p.write_text(json.dumps({"corp_id": CORP, "agent_id": 1000002,
                             "app_secret": SECRET, "cookie": "not-permitted"}))
    with pytest.raises(WeComError, match="credentials_invalid"):
        load_credentials(p)


@pytest.mark.parametrize("corp,agent,secret", [("wrong", 1, SECRET), (CORP, True, SECRET),
                                               (CORP, -1, SECRET), (CORP, 1, "bad\nsecret")])
def test_validate_credentials(corp, agent, secret):
    with pytest.raises(WeComError):
        Credentials(corp, agent, secret)


def test_readonly_fixed_host_cache_and_safe_report(caplog):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "GET" and request.url.host == "qyapi.weixin.qq.com"
        value = token_result() if request.url.path.endswith("gettoken") else app_result()
        return httpx.Response(200, json=value)
    with caplog.at_level(logging.DEBUG):
        api = client(handler)
        report = api.doctor()
        api.doctor()
        api.close()
    assert [r.url.path for r in requests] == ["/cgi-bin/gettoken", "/cgi-bin/agent/get", "/cgi-bin/agent/get"]
    assert report["visible_member_count"] == 1
    assert report["customer_data_read"] is False
    assert report["customer_contact_authorization_verified"] is False
    assert SECRET not in caplog.text and TOKEN not in caplog.text
    assert "private-member" not in json.dumps(report)


def test_no_arbitrary_route():
    api = client(lambda _: pytest.fail("must not call network"))
    with pytest.raises(WeComError, match="route_rejected"):
        api._get("/message/send", {})
    api.close()


def test_unsafe_redirect_is_not_followed():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://other.example/steal"})
    api = client(handler)
    with pytest.raises(WeComError, match="http_rejected"):
        api.doctor()
    api.close()
    assert len(seen) == 1


def test_transient_network_retries_bounded_without_leaking():
    seen = []
    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout(str(request.url), request=request)
    api = client(handler)
    with pytest.raises(WeComError, match="network_unavailable") as error:
        api.doctor()
    api.close()
    assert len(seen) == 2
    assert SECRET not in str(error.value)


def test_transient_http_recovers_once():
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=token_result() if request.url.path.endswith("gettoken") else app_result())
    api = client(handler)
    assert api.doctor()["status"] == "ok"
    api.close()
    assert len(seen) == 3


def test_expired_token_refreshes_only_once():
    seen = []
    def handler(request):
        seen.append(request)
        result = token_result() if request.url.path.endswith("gettoken") else {"errcode": 42001, "errmsg": TOKEN}
        return httpx.Response(200, json=result)
    api = client(handler)
    with pytest.raises(WeComError) as error:
        api.doctor()
    api.close()
    assert len(seen) == 4 and error.value.api_code == 42001
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize("value", [{}, [], {"errcode": False}, {"errcode": 0},
                                    {"errcode": 0, "access_token": "x", "expires_in": True}])
def test_malformed_auth_response(value):
    api = client(lambda _: httpx.Response(200, json=value))
    with pytest.raises(WeComError, match="response_invalid"):
        api.doctor()
    api.close()


@pytest.mark.parametrize("values", [{"agentid": 42}, {"close": None}, {"allow_userinfos": []},
                                     {"allow_partys": {"partyid": "not-list"}}])
def test_malformed_or_wrong_app(values):
    api = client(lambda r: httpx.Response(200, json=token_result() if r.url.path.endswith("gettoken")
                                         else app_result(**values)))
    with pytest.raises(WeComError):
        api.doctor()
    api.close()


def test_response_size_bounded():
    api = client(lambda _: httpx.Response(200, content=b"x" * (MAX_RESPONSE + 1)))
    with pytest.raises(WeComError, match="response_too_large"):
        api.doctor()
    api.close()


def test_provision_requires_interactive_tty(monkeypatch, capsys):
    monkeypatch.setattr(os, "isatty", lambda _: False)
    assert main(["provision", "--corp-id", CORP, "--agent-id", "1000002"]) == 1
    assert "interactive_terminal_required" in capsys.readouterr().out
