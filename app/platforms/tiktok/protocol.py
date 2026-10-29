"""TikTok Web API contracts and signature diagnostics.

TikTok changes its Web signer independently of CreatorHub.  The stable
integration boundary is therefore the authenticated browser page: the page
creates dynamic anti-bot parameters, while CreatorHub observes the signed
request and parses the JSON response.  This module intentionally records only
field presence/length/location; it never persists token or signature values.
CreatorHub also carries a pure-Python signer for read-only native API calls;
the authenticated page remains the compatibility fallback.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit


SIGNATURE_FIELDS = ("msToken", "X-Bogus", "X-Gnarly", "X-Dynosaur")

API_CONTRACTS = (
    {
        "name": "video_search",
        "paths": ("/api/search/item/full/", "/api/search/general/preview/",
                  "/api/search/general/full/"),
        "method": "GET",
        "trigger": "打开 /search/video?q= 并滚动",
    },
    {
        "name": "user_search",
        "paths": ("/api/search/user/full/",),
        "method": "GET",
        "trigger": "打开 /search/user?q= 并滚动",
    },
    {
        "name": "profile_works",
        "paths": ("/api/post/item_list/", "/api/user/list/"),
        "method": "GET",
        "trigger": "打开 /@<unique_id> 并滚动",
    },
    {
        "name": "comment_list",
        "paths": ("/api/comment/list/",),
        "method": "GET",
        "trigger": "打开作品详情页",
    },
    {
        "name": "comment_reply_list",
        "paths": ("/api/comment/list/reply/",),
        "method": "GET",
        "trigger": "展开 View more replies",
    },
    {
        "name": "comment_publish",
        "paths": ("/api/comment/publish/", "/api/comment/create/"),
        "method": "POST",
        "trigger": "页面评论输入框提交",
    },
)


def classify_tiktok_api(url: str) -> str:
    path = urlsplit(str(url or "")).path.lower()
    for contract in API_CONTRACTS:
        if any(anchor.lower() in path for anchor in contract["paths"]):
            return str(contract["name"])
    return ""


def signed_request_evidence(url: str, headers: dict | None = None) -> dict:
    """Return non-secret evidence for one TikTok request."""
    raw_url = str(url or "")
    split = urlsplit(raw_url)
    query = {str(key).lower(): str(value) for key, value in parse_qsl(
        split.query, keep_blank_values=True)}
    normalized_headers = {
        str(key).lower(): str(value)
        for key, value in (headers or {}).items()
    }
    fields = {}
    for field in SIGNATURE_FIELDS:
        key = field.lower()
        value = query.get(key)
        location = "query"
        if value is None:
            value = normalized_headers.get(key)
            location = "header"
        fields[field] = {
            "present": value is not None,
            "location": location if value is not None else "",
            "length": len(value or ""),
        }
    return {
        "endpoint": classify_tiktok_api(raw_url),
        "path": split.path,
        "fields": fields,
        "signed": any(item["present"] for item in fields.values()),
    }


def protocol_summary() -> dict:
    return {
        "mode": "auto",
        "signer": "offline_python + page_runtime_fallback",
        "offline_algorithm": True,
        "signature_fields": list(SIGNATURE_FIELDS),
        "contracts": [dict(item) for item in API_CONTRACTS],
        "algorithm": {
            "input": "URL 查询、Cookie、UA、浏览器指纹、时间与页面 SDK 状态",
            "execution": "纯 Python 生成 X-Dynosaur/X-Gnarly；页面 SDK 保留兜底",
            "output": list(SIGNATURE_FIELDS),
            "replay_rule": "每次请求重新生成并使用本账号 msToken/UA/代理；签名 URL 禁止二次编码",
        },
    }
