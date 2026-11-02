import json
import logging

import httpx
import pytest

from app.wecom_readonly import Credentials, MAX_RESPONSE, WeComError
from app.wecom_customer_readonly import WeComCustomerReadOnly, main


MEMBER = "private-test-member"
CUSTOMER = "private-customer-id"
SECRET = "test-secret-not-live"
TOKEN = "test-token-not-live"


def app(**overrides):
    return {"errcode": 0, "agentid": 1000002, "close": 0,
            "allow_userinfos": {"user": [{"userid": MEMBER}]}, **overrides}


def response(request, overrides=None):
    route = request.url.path.rsplit("/", 1)[-1]
    values = {
        "gettoken": {"errcode": 0, "access_token": TOKEN, "expires_in": 7200},
        "get": app(), "get_follow_user_list": {"errcode": 0, "follow_user": [MEMBER]},
        "list": {"errcode": 0, "external_userid": [CUSTOMER]},
    }
    return httpx.Response(200, json=(overrides or {}).get(route, values[route]))


def client(handler):
    return WeComCustomerReadOnly(Credentials("wwtest0000000000", 1000002, SECRET),
                                transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_explicit_approval_required_before_any_network():
    api = client(lambda _: pytest.fail("no network before approval"))
    with pytest.raises(WeComError, match="approval_required"):
        api.probe()
    api.close()


def test_counts_only_fixed_host_single_member_and_get_only(caplog):
    seen = []
    def handler(request):
        seen.append(request)
        assert request.method == "GET" and request.url.host == "qyapi.weixin.qq.com"
        if request.url.path.endswith("/list"):
            assert request.url.params["userid"] == MEMBER
        return response(request)
    with caplog.at_level(logging.DEBUG):
        api = client(handler)
        report = api.probe(acknowledge_customer_ids=True)
        api.close()
    assert report["status"] == "ok" and report["customer_ids_returned"] == 1
    assert report["customer_details_read"] is False and report["chat_content_read"] is False
    assert len(seen) == 4 and api._token == ""
    for private in [MEMBER, CUSTOMER, SECRET, TOKEN]:
        assert private not in json.dumps(report) and private not in caplog.text


@pytest.mark.parametrize("scope", [
    {"allow_userinfos": {"user": []}},
    {"allow_userinfos": {"user": [{"userid": MEMBER}, {"userid": "other"}]}},
    {"allow_userinfos": {"user": [MEMBER]}},
    {"allow_partys": {"partyid": [1]}}, {"allow_tags": {"tagid": [1]}},
    {"allow_userinfos": []}, {"agentid": 42}, {"close": 1}, {"close": False},
])
def test_scope_drift_stops_before_customer_endpoints(scope):
    seen = []
    def handler(request):
        seen.append(request.url.path)
        assert "externalcontact" not in request.url.path
        return response(request, {"get": app(**scope)})
    api = client(handler)
    with pytest.raises(WeComError):
        api.probe(acknowledge_customer_ids=True)
    api.close()
    assert len(seen) == 2


def test_unexpected_enabled_member_stops_before_customer_list():
    seen = []
    def handler(request):
        seen.append(request.url.path)
        return response(request, {"get_follow_user_list": {"errcode": 0, "follow_user": ["other"]}})
    api = client(handler)
    with pytest.raises(WeComError, match="scope_changed"):
        api.probe(acknowledge_customer_ids=True)
    api.close()
    assert len(seen) == 3


def test_disabled_contact_member_does_not_claim_list_acceptance():
    api = client(lambda r: response(r, {"get_follow_user_list": {"errcode": 0, "follow_user": []}}))
    report = api.probe(acknowledge_customer_ids=True)
    api.close()
    assert report["status"] == "test_member_not_customer_contact_enabled"
    assert report["customer_id_list_requested"] is False and report["customer_ids_returned"] is None


@pytest.mark.parametrize("ids", [None, "wrong", [1], [""], ["x\n"], ["x", "x"]])
def test_invalid_customer_id_list_is_rejected(ids):
    api = client(lambda r: response(r, {"list": {"errcode": 0, "external_userid": ids}}))
    with pytest.raises(WeComError, match="response_invalid"):
        api.probe(acknowledge_customer_ids=True)
    api.close()


def test_empty_customer_list_is_valid_not_sample_data_acceptance():
    api = client(lambda r: response(r, {"list": {"errcode": 0, "external_userid": []}}))
    report = api.probe(acknowledge_customer_ids=True)
    api.close()
    assert report["status"] == "ok" and report["customer_ids_returned"] == 0


def test_no_relationship_preserves_partial_success_without_claiming_empty_list():
    api = client(lambda r: response(r, {"list": {"errcode": 84061, "errmsg": CUSTOMER}}))
    report = api.probe(acknowledge_customer_ids=True)
    api.close()
    assert report["status"] == "no_external_contact_relationship"
    assert report["customer_members_api_verified"] is True
    assert report["enabled_member_count"] == 1
    assert report["customer_id_list_requested"] is True
    assert report["customer_list_api_verified"] is False
    assert report["customer_ids_returned"] is None
    assert report["customer_list_api_code"] == 84061
    assert CUSTOMER not in json.dumps(report)


@pytest.mark.parametrize("route", ["/externalcontact/get", "/message/send", "/externalcontact/add_msg_template"])
def test_no_details_or_write_route(route):
    api = client(lambda _: pytest.fail("network prohibited"))
    with pytest.raises(WeComError, match="route_rejected"):
        api._external_get(route, {})
    api.close()


def test_redirect_is_not_followed():
    seen = []
    def handler(request):
        seen.append(request)
        if "externalcontact" in request.url.path:
            return httpx.Response(302, headers={"location": "https://other.example/steal"})
        return response(request)
    api = client(handler)
    with pytest.raises(WeComError, match="http_rejected"):
        api.probe(acknowledge_customer_ids=True)
    api.close()
    assert len(seen) == 3


def test_network_failure_is_bounded_and_redacted():
    seen = []
    def handler(request):
        seen.append(request)
        if "externalcontact" in request.url.path:
            raise httpx.ReadTimeout(str(request.url), request=request)
        return response(request)
    api = client(handler)
    with pytest.raises(WeComError, match="network_unavailable") as error:
        api.probe(acknowledge_customer_ids=True)
    api.close()
    assert len(seen) == 4 and TOKEN not in str(error.value)


def test_expired_external_token_refreshes_once_only():
    seen = []
    def handler(request):
        seen.append(request)
        if "externalcontact" in request.url.path:
            return httpx.Response(200, json={"errcode": 42001, "errmsg": TOKEN})
        return response(request)
    api = client(handler)
    with pytest.raises(WeComError) as error:
        api.probe(acknowledge_customer_ids=True)
    api.close()
    assert error.value.api_code == 42001 and len(seen) == 5


def test_external_response_size_bounded():
    api = client(lambda r: httpx.Response(200, content=b"x" * (MAX_RESPONSE + 1))
                 if "externalcontact" in r.url.path else response(r))
    with pytest.raises(WeComError, match="response_too_large"):
        api.probe(acknowledge_customer_ids=True)
    api.close()


def test_cli_missing_acknowledgment_does_not_load_credentials(monkeypatch, capsys):
    monkeypatch.setattr("app.wecom_customer_readonly.load_credentials", lambda _: pytest.fail("not approved"))
    assert main([]) == 1
    assert "approval_required" in capsys.readouterr().out
