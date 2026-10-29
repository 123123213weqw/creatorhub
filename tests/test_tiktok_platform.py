from app.platforms.tiktok import (
    flatten_tiktok_comments,
    parse_tiktok_aweme,
    parse_tiktok_app_context,
    parse_tiktok_comment,
    parse_tiktok_user,
)
from app.platforms.tiktok.urls import parse_video_reference, video_url
from app.browser.tiktok_fetcher import (
    _search_items_from_payload,
    _wait_for_tiktok_search_results,
)
from app.platforms.tiktok.protocol import (
    classify_tiktok_api,
    protocol_summary,
    signed_request_evidence,
)
import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from app.platforms.tiktok.client import TikTokNativeClient, search_pairs
from app.platforms.tiktok.client import comment_pairs, comment_reply_pairs
from app.platforms.tiktok.offline_sign import (
    BOGUS_VALUE,
    hash_state,
    pick_ms_token,
    seal,
    sign,
    unseal,
)


def test_parse_tiktok_aweme_camel_case_payload():
    aweme = parse_tiktok_aweme({
        "item": {
            "id": "731",
            "desc": "hello",
            "createTime": "1700000000",
            "author": {
                "nickname": "Alice",
                "uniqueId": "alice",
                "avatarThumb": {"urlList": ["https://img/avatar.jpg"]},
            },
            "stats": {"diggCount": 12, "commentCount": 3},
            "video": {
                "duration": 14,
                "playAddr": {"urlList": ["https://video/play.mp4"]},
                "cover": {"urlList": ["https://img/cover.jpg"]},
            },
        }
    })
    assert aweme is not None
    assert aweme.platform == "tiktok"
    assert aweme.aweme_id == "731"
    assert aweme.medias[0].url == "https://video/play.mp4"
    assert aweme.like_count == 12
    assert aweme.comment_count == 3
    assert aweme.author_unique_id == "alice"
    assert aweme.source_url == "https://www.tiktok.com/@alice/video/731"


def test_parse_tiktok_aweme_keeps_metadata_without_download_url():
    aweme = parse_tiktok_aweme({
        "item": {
            "id": "732", "desc": "rendered card",
            "author": {"uniqueId": "alice"},
            "stats": {"diggCount": 2},
            "video": {"cover": {"urlList": ["https://img/cover.jpg"]}},
        }
    })
    assert aweme is not None
    assert aweme.aweme_id == "732"
    assert aweme.medias == []
    assert aweme.source_url == "https://www.tiktok.com/@alice/video/732"


def test_tiktok_search_payload_walks_hydration_shapes():
    rows = _search_items_from_payload({
        "__DEFAULT_SCOPE__": {
            "webapp.search-video": {
                "itemList": [{
                    "itemStruct": {
                        "id": "733",
                        "author": {"uniqueId": "bob"},
                        "video": {},
                    }
                }]
            }
        }
    })
    assert len(rows) == 1
    assert (rows[0].get("itemStruct") or {})["id"] == "733"


def test_comment_reply_relationship_and_flatten():
    rows = flatten_tiktok_comments([{
        "cid": "root",
        "text": "root text",
        "reply_comment": [{"cid": "reply", "text": "reply text"}],
    }])
    parsed = [parse_tiktok_comment(row) for row in rows]
    assert parsed[0]["reply_to"] == ""
    assert parsed[1]["reply_to"] == "root"


def test_parse_tiktok_user_search_shape():
    value = parse_tiktok_user({
        "user_info": {
            "user": {
                "id": "1", "secUid": "sec", "uniqueId": "alice",
                "nickname": "Alice", "signature": "bio",
            },
            "stats": {"followerCount": 23, "videoCount": 4},
        }
    })
    assert value["unique_id"] == "alice"
    assert value["sec_uid"] == "sec"
    assert value["follower_count"] == 23


def test_parse_tiktok_standard_video_reference_and_hydrated_self_profile():
    ref = parse_video_reference("share https://www.tiktok.com/@alice/video/731?lang=en")
    assert ref.aweme_id == "731"
    assert ref.author_unique_id == "alice"
    assert ref.url == "https://www.tiktok.com/@alice/video/731"
    assert video_url("731", "@alice") == ref.url
    assert parse_video_reference("731") is None

    profile = parse_tiktok_app_context({
        "user": {
            "uid": "1", "secUid": "sec", "uniqueId": "alice",
            "nickname": "Alice", "avatarLarger": {"urlList": ["https://img/a.jpg"]},
        },
        "userStats": {"followerCount": 5, "followingCount": 2, "videoCount": 3},
    })
    assert profile["unique_id"] == "alice"
    assert profile["avatar"] == "https://img/a.jpg"
    assert profile["follower_count"] == 5


def test_tiktok_protocol_recognizes_preview_and_redacts_signature_values():
    evidence = signed_request_evidence(
        "https://www.tiktok.com/api/search/general/preview/?msToken=secret-token"
        "&X-Bogus=bogus-value&X-Gnarly=gnarly-value",
        {"X-Dynosaur": "dynosaur-value"},
    )
    assert classify_tiktok_api(evidence["path"]) == "video_search"
    assert evidence["endpoint"] == "video_search"
    assert evidence["signed"] is True
    assert evidence["fields"]["msToken"] == {
        "present": True, "location": "query", "length": 12,
    }
    assert evidence["fields"]["X-Dynosaur"]["location"] == "header"
    assert "secret-token" not in repr(evidence)
    summary = protocol_summary()
    assert summary["mode"] == "auto"
    assert summary["signer"] == "offline_python + page_runtime_fallback"
    assert summary["offline_algorithm"] is True


def test_tiktok_offline_signer_roundtrips_and_preserves_parameter_order():
    key = tuple(range(12))
    query, params = sign(
        [("browser_version", "5.0 (Windows)"), ("q", "中")],
        "Mozilla/5.0",
        ms_token="session-token",
        timestamp=1788856077,
        nonce=9,
        nonce2=11,
        key=key,
        key2=tuple(range(12, 24)),
        rng=random.Random(11),
    )
    assert [part.split("=", 1)[0] for part in query.split("&")] == [
        "browser_version", "q", "X-Dynosaur", "msToken", "X-Bogus", "X-Gnarly",
    ]
    assert query.startswith("browser_version=5.0%20(Windows)&q=%E4%B8%AD")
    assert params["X-Bogus"] == BOGUS_VALUE == "1"
    recovered, recovered_key = unseal(params["X-Dynosaur"])
    assert seal(recovered, recovered_key) == params["X-Dynosaur"]
    assert recovered_key == key
    assert hash_state("aid=1988&count=10") == 0x52B0CB44


def test_tiktok_offline_signer_uses_only_identity_ms_token():
    assert pick_ms_token(None) == ""
    assert pick_ms_token({"ttwid": "x"}) == ""
    assert pick_ms_token({"msToken": "account-token"}) == "account-token"


def test_tiktok_native_search_params_are_ordered_and_identity_bound():
    pairs = search_pairs(
        "creator tools", count=20, user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/156.0.0.0 Safari/537.36"),
        locale="en-US", timezone="America/New_York",
        viewport=(1536, 864), device_seed="account-6",
    )
    params = dict(pairs)
    assert pairs[0] == ("aid", "1988")
    assert params["keyword"] == "creator tools"
    assert params["device_id"].isdigit() and len(params["device_id"]) == 19
    assert params["browser_version"] == "5.0 (Windows)"
    assert params["screen_width"] == "1536"


def test_tiktok_native_client_sends_signed_url_without_params_reencoding():
    class Response:
        status_code = 200
        content = b'{"item_list": [{"itemStruct": {"id": "731", "author": {"uniqueId": "alice"}}}]}'
        headers = {}

        def json(self):
            import json
            return json.loads(self.content)

    session = AsyncMock()
    session.get.return_value = Response()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    state = {"cookies": [{"name": "msToken", "value": "account-token", "domain": ".tiktok.com"}]}
    client = TikTokNativeClient(
        storage_state=state,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/156.0.0.0",
        device_seed="account-6",
    )
    with patch("app.platforms.tiktok.client.AsyncSession", return_value=session):
        result = asyncio.run(client.search("creator tools", count=5))
    assert result.category == "success"
    assert len(result.items) == 1
    url = session.get.await_args.args[0]
    assert "&msToken=account-token&X-Bogus=1&X-Gnarly=" in url
    assert session.get.await_args.kwargs["quote"] is False
    assert "params" not in session.get.await_args.kwargs


def test_tiktok_native_client_percent_encodes_cjk_referer():
    class Response:
        status_code = 200
        content = b'{"item_list": []}'
        headers = {}

        def json(self):
            import json
            return json.loads(self.content)

    session = AsyncMock()
    session.get.return_value = Response()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    client = TikTokNativeClient(
        storage_state={},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/156.0.0.0",
    )
    with patch("app.platforms.tiktok.client.AsyncSession", return_value=session) as session_cls:
        result = asyncio.run(client.search("电脑显卡", count=5))
    assert result.category == "empty_valid"
    headers = session_cls.call_args.kwargs["headers"]
    assert headers["Referer"].endswith(
        "q=%E7%94%B5%E8%84%91%E6%98%BE%E5%8D%A1")
    headers["Referer"].encode("latin-1")


def test_tiktok_native_comment_params_preserve_endpoint_names():
    common = dict(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        device_seed="account-6",
    )
    params = dict(comment_pairs("731", cursor=2, count=5, **common))
    assert params["aweme_id"] == "731"
    assert params["cursor"] == "2"
    assert params["count"] == "5"
    assert params["from_page"] == "video"
    replies = dict(comment_reply_pairs(
        "731", "comment-1", cursor=3, count=4, **common))
    assert "aweme_id" not in replies
    assert replies["item_id"] == "731"
    assert replies["comment_id"] == "comment-1"


def test_tiktok_native_client_fetches_comments_without_browser():
    responses = []

    class Response:
        status_code = 200
        headers = {}

        def __init__(self, payload):
            import json
            self._payload = payload
            self.content = json.dumps(payload).encode()

        def json(self):
            return self._payload

    responses.append(Response({
        "status_code": 0, "has_more": 0, "cursor": 0,
        "comments": [{
            "cid": "c1", "aweme_id": "731", "text": "hello",
            "user": {"nickname": "Alice", "sec_uid": "sec"},
            "reply_comment_total": 0,
        }],
    }))
    session = AsyncMock()
    session.get.side_effect = responses
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    client = TikTokNativeClient(
        storage_state={},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/156.0.0.0",
    )
    with patch("app.platforms.tiktok.client.AsyncSession", return_value=session):
        result = asyncio.run(client.comments(
            "731", count=5, target_url="https://www.tiktok.com/@alice/video/731"))
    assert result.category == "success"
    assert [row["cid"] for row in result.items] == ["c1"]
    url = session.get.await_args.args[0]
    assert "/api/comment/list/?" in url
    assert "aweme_id=731" in url
    assert session.get.await_args.kwargs["quote"] is False


def test_tiktok_search_waits_for_delayed_rendered_cards():
    state = {"reads": 0}

    async def evaluate(script):
        if "querySelectorAll" not in script:
            return []
        state["reads"] += 1
        if state["reads"] < 4:
            return []
        return [{
            "item": {
                "id": "7686904096200117512",
                "author": {"uniqueId": "zachhh.mp4"},
                "video": {},
            }
        }]

    page = SimpleNamespace(
        evaluate=AsyncMock(side_effect=evaluate),
        wait_for_timeout=AsyncMock(),
    )
    collected = {}
    found = asyncio.run(_wait_for_tiktok_search_results(
        page, collected, timeout_ms=5000))
    assert found is True
    assert "7686904096200117512" in collected
    assert page.wait_for_timeout.await_count == 3
