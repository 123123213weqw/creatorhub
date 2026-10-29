"""Direct TikTok Web reads using CreatorHub's offline signer.

The signed query is passed to curl_cffi with ``quote=False`` and no ``params``
mapping. Any re-encoding after :func:`offline_sign.sign` invalidates X-Gnarly.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping
from urllib.parse import quote

from curl_cffi.requests import AsyncSession

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua
from .extract import flatten_tiktok_comments
from .offline_sign import BOGUS_VALUE, SDK_VERSION, SCM_VERSION, pick_ms_token, sign

BASE = "https://www.tiktok.com"
SEARCH_ITEM_PATH = "/api/search/item/full/"
COMMENT_LIST_PATH = "/api/comment/list/"
COMMENT_REPLY_PATH = "/api/comment/list/reply/"


def _cookie_map(storage_state: str | Mapping | None) -> dict[str, str]:
    if isinstance(storage_state, str):
        try:
            storage_state = json.loads(storage_state or "{}")
        except (TypeError, ValueError):
            storage_state = {}
    if not isinstance(storage_state, Mapping):
        return {}
    output: dict[str, str] = {}
    for cookie in storage_state.get("cookies", []) or []:
        if not isinstance(cookie, Mapping):
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domain and not (domain == "tiktok.com" or domain.endswith(".tiktok.com")):
            continue
        name = str(cookie.get("name") or "")
        if name and name not in output:
            output[name] = str(cookie.get("value") or "")
    return output


def _app_version(user_agent: str) -> str:
    matched = re.search(r"Mozilla/([\d.]+) \(([^;)]+)", user_agent or "")
    if not matched:
        return "5.0 (Windows)"
    return f"{matched.group(1)} ({matched.group(2).split()[0]})"


def _os_name(user_agent: str) -> str:
    value = (user_agent or "").lower()
    if "macintosh" in value or "mac os" in value:
        return "mac"
    if "linux" in value and "android" not in value:
        return "linux"
    return "windows"


def _device_id(seed: str) -> str:
    source = random.Random(f"tiktok-device:{seed}")
    return str(source.randint(1, 9)) + "".join(
        str(source.randint(0, 9)) for _ in range(18)
    )


def search_pairs(
    keyword: str, *, count: int, offset: int = 0, user_agent: str,
    locale: str = "en-US", timezone: str = "America/New_York",
    viewport: tuple[int, int] = (1920, 1080), region: str = "US",
    device_seed: str = "creatorhub",
) -> list[tuple[str, str]]:
    language = (locale or "en-US").split("-", 1)[0] or "en"
    width, height = viewport
    values = {
        "aid": "1988",
        "app_language": language,
        "app_name": "tiktok_web",
        "browser_language": locale or "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32" if _os_name(user_agent) == "windows" else "MacIntel",
        "browser_version": _app_version(user_agent),
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "device_id": _device_id(device_seed),
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "search",
        "history_len": "4",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "language": language,
        "os": _os_name(user_agent),
        "priority_region": region or "US",
        "referer": "",
        "region": region or "US",
        "root_referer": "https://www.tiktok.com/",
        "screen_height": str(height or 1080),
        "screen_width": str(width or 1920),
        "tz_name": timezone or "America/New_York",
        "webcast_language": language,
        "keyword": str(keyword or "").strip(),
        "offset": str(max(0, int(offset))),
        "count": str(max(1, min(30, int(count)))),
        "search_source": "normal_search",
    }
    if not values["keyword"]:
        raise ValueError("TikTok 搜索关键词不能为空")
    return list(values.items())


def comment_pairs(
    aweme_id: str, *, cursor: str | int = "0", count: int = 20,
    user_agent: str, locale: str = "en-US",
    timezone: str = "America/New_York",
    viewport: tuple[int, int] = (1920, 1080), region: str = "US",
    device_seed: str = "creatorhub",
) -> list[tuple[str, str]]:
    pairs = search_pairs(
        "x", count=1, user_agent=user_agent, locale=locale,
        timezone=timezone, viewport=viewport, region=region,
        device_seed=device_seed,
    )
    output = [
        (key, "video" if key == "from_page" else value)
        for key, value in pairs
        if key not in {"keyword", "offset", "search_source"}
    ]
    output.extend((
        ("aweme_id", str(aweme_id)),
        ("cursor", str(cursor or "0")),
        ("count", str(max(1, min(50, int(count))))),
        ("current_region", region or "US"),
    ))
    return output


def comment_reply_pairs(
    item_id: str, comment_id: str, *, cursor: str | int = "0",
    count: int = 20, user_agent: str, locale: str = "en-US",
    timezone: str = "America/New_York",
    viewport: tuple[int, int] = (1920, 1080), region: str = "US",
    device_seed: str = "creatorhub",
) -> list[tuple[str, str]]:
    pairs = comment_pairs(
        item_id, cursor=cursor, count=count, user_agent=user_agent,
        locale=locale, timezone=timezone, viewport=viewport,
        region=region, device_seed=device_seed,
    )
    tail = dict(pairs)
    output = [
        (key, value) for key, value in pairs
        if key not in {"aweme_id", "cursor", "count", "current_region"}
    ]
    output.extend((
        ("item_id", str(item_id)),
        ("comment_id", str(comment_id)),
        ("cursor", tail["cursor"]),
        ("count", tail["count"]),
        ("current_region", tail["current_region"]),
    ))
    return output


def _rows(payload) -> list[dict]:
    output: dict[str, dict] = {}
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        item = value.get("item") or value.get("aweme_info") \
            or value.get("itemStruct") or value
        if isinstance(item, dict):
            item_id = str(item.get("id") or item.get("aweme_id") or item.get("itemId") or "")
            author = item.get("author")
            if item_id and isinstance(author, dict):
                output.setdefault(item_id, value)
        stack.extend(child for child in value.values() if isinstance(child, (dict, list)))
    return list(output.values())


@dataclass
class TikTokNativeResult:
    items: list[dict] = field(default_factory=list)
    error: str = ""
    category: str = "success"
    http_status: int = 0
    body_length: int = 0
    tt_orcas_res: str = ""
    endpoint: str = SEARCH_ITEM_PATH
    signature_lengths: dict[str, int] = field(default_factory=dict)

    @property
    def compatible(self) -> bool:
        return self.category in {"success", "empty_valid"}


class TikTokNativeClient:
    def __init__(
        self, *, storage_state: str | Mapping | None, user_agent: str,
        proxy: str = "", locale: str = "en-US",
        timezone: str = "America/New_York", viewport: tuple[int, int] = (1920, 1080),
        region: str = "US", device_seed: str = "creatorhub", timeout: float = 30.0,
    ):
        self.cookies = _cookie_map(storage_state)
        self.user_agent = str(user_agent or "")
        self.proxy = normalize_proxy(proxy) or None
        self.locale = locale or "en-US"
        self.timezone = timezone or "America/New_York"
        self.viewport = viewport
        self.region = region or "US"
        self.device_seed = device_seed or "creatorhub"
        self.timeout = float(timeout)
        self.impersonate = impersonate_for_ua(self.user_agent)

    async def _get_json(
        self, path: str, pairs: Iterable[tuple[str, str]], *, referer: str,
    ) -> tuple[TikTokNativeResult, object | None]:
        query, added = sign(
            pairs, self.user_agent, ms_token=pick_ms_token(self.cookies)
        )
        url = f"{BASE}{path}?{query}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": f"{self.locale},en;q=0.8",
            "Referer": referer,
            "User-Agent": self.user_agent,
        }
        result = TikTokNativeResult(signature_lengths={
            key: len(value) for key, value in added.items()
        }, endpoint=path)
        try:
            async with AsyncSession(
                impersonate=self.impersonate, cookies=self.cookies, headers=headers
            ) as session:
                response = await session.get(
                    url, quote=False, proxy=self.proxy, timeout=self.timeout,
                    allow_redirects=False,
                )
        except Exception as exc:
            result.category = "network_failure"
            result.error = f"TikTok 离线接口网络失败: {exc}"
            return result, None
        raw = bytes(response.content or b"")
        result.http_status = int(response.status_code or 0)
        result.body_length = len(raw)
        result.tt_orcas_res = str((response.headers or {}).get("tt_orcas_res") or "")
        location = str((response.headers or {}).get("location") or "").lower()
        if result.http_status in {301, 302, 303, 307, 308} and "login" in location:
            result.category = "auth_failure"
            result.error = "TikTok 离线接口登录态失效"
            return result, None
        if result.http_status in {401, 403}:
            result.category = "auth_failure"
            result.error = f"TikTok 离线接口鉴权失败: HTTP {result.http_status}"
            return result, None
        if result.http_status != 200:
            result.category = "http_failure"
            result.error = f"TikTok 离线接口失败: HTTP {result.http_status}"
            return result, None
        if not raw:
            result.category = "signature_rejection" if result.tt_orcas_res else "empty_invalid"
            result.error = ("TikTok 离线签名/环境被拒绝" if result.tt_orcas_res
                            else "TikTok 离线接口返回空响应")
            return result, None
        try:
            payload = response.json()
        except Exception:
            text = raw[:2000].decode("utf-8", "ignore").lower()
            if any(word in text for word in ("captcha", "verify to continue", "security verification")):
                result.category = "verification_required"
                result.error = "TikTok 离线接口要求安全验证"
            else:
                result.category = "invalid_json"
                result.error = "TikTok 离线接口返回非 JSON 数据"
            return result, None
        status_code = payload.get("status_code", payload.get("statusCode")) if isinstance(payload, dict) else None
        if status_code not in (None, 0, "0"):
            result.category = "api_rejection"
            result.error = f"TikTok 离线接口业务拒绝: {status_code}"
            return result, payload
        return result, payload

    async def search(self, keyword: str, *, count: int = 20, offset: int = 0) -> TikTokNativeResult:
        pairs = search_pairs(
            keyword, count=count, offset=offset, user_agent=self.user_agent,
            locale=self.locale, timezone=self.timezone, viewport=self.viewport,
            region=self.region, device_seed=self.device_seed,
        )
        # curl_cffi headers are byte strings. A raw CJK keyword reaches its
        # latin-1 encoder and fails before the request is sent, which made auto
        # mode incorrectly open the browser. Browsers encode it in the URL.
        referer = f"{BASE}/search/video?q={quote(str(keyword), safe='')}"
        result, payload = await self._get_json(
            SEARCH_ITEM_PATH, pairs, referer=referer)
        if payload is None or not result.compatible:
            return result
        result.items = _rows(payload)[:max(1, int(count))]
        if not result.items:
            result.category = "empty_valid"
        return result

    async def comments(
        self, aweme_id: str, *, count: int = 20,
        include_replies: bool = False, author_unique_id: str = "",
        target_url: str = "",
    ) -> TikTokNativeResult:
        limit = max(1, int(count))
        referer = str(target_url or "").strip() or (
            f"{BASE}/@{author_unique_id or 'user'}/video/{aweme_id}")
        collected: dict[str, dict] = {}
        cursor = "0"
        total_bytes = 0
        final = TikTokNativeResult(endpoint=COMMENT_LIST_PATH)
        for _ in range(max(1, min(20, (limit + 19) // 20 + 1))):
            pairs = comment_pairs(
                aweme_id, cursor=cursor, count=min(20, limit),
                user_agent=self.user_agent, locale=self.locale,
                timezone=self.timezone, viewport=self.viewport,
                region=self.region, device_seed=self.device_seed,
            )
            page, payload = await self._get_json(
                COMMENT_LIST_PATH, pairs, referer=referer)
            total_bytes += page.body_length
            final = page
            if payload is None or not page.compatible:
                break
            roots = payload.get("comments") if isinstance(payload, dict) else []
            roots = roots if isinstance(roots, list) else []
            for raw in flatten_tiktok_comments(
                    roots, include_replies=include_replies):
                cid = str(raw.get("cid") or raw.get("id") or raw.get("comment_id") or "")
                if cid:
                    collected.setdefault(cid, raw)
                if len(collected) >= limit:
                    break
            if include_replies and len(collected) < limit:
                for root in roots:
                    parent_id = str(root.get("cid") or root.get("id") or "")
                    inline = root.get("reply_comment") or []
                    reply_total = int(root.get("reply_comment_total") or 0)
                    if not parent_id or reply_total <= len(inline):
                        continue
                    reply_cursor = "0"
                    for _reply_page in range(max(1, min(20, (reply_total + 19) // 20))):
                        reply_pairs = comment_reply_pairs(
                            aweme_id, parent_id, cursor=reply_cursor,
                            count=min(20, limit - len(collected)),
                            user_agent=self.user_agent, locale=self.locale,
                            timezone=self.timezone, viewport=self.viewport,
                            region=self.region, device_seed=self.device_seed,
                        )
                        reply_result, reply_payload = await self._get_json(
                            COMMENT_REPLY_PATH, reply_pairs, referer=referer)
                        total_bytes += reply_result.body_length
                        if reply_payload is None or not reply_result.compatible:
                            final = reply_result
                            break
                        replies = reply_payload.get("comments") if isinstance(reply_payload, dict) else []
                        replies = replies if isinstance(replies, list) else []
                        for raw in replies:
                            value = dict(raw)
                            value.setdefault("reply_id", parent_id)
                            cid = str(value.get("cid") or value.get("id") or "")
                            if cid:
                                collected.setdefault(cid, value)
                            if len(collected) >= limit:
                                break
                        if len(collected) >= limit or not reply_payload.get("has_more"):
                            break
                        reply_cursor = str(reply_payload.get("cursor") or "")
                        if not reply_cursor:
                            break
                    if len(collected) >= limit:
                        break
            if len(collected) >= limit or not payload.get("has_more"):
                break
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
        final.items = list(collected.values())[:limit]
        final.body_length = total_bytes
        if final.items:
            final.category = "success"
            final.error = ""
        elif final.compatible:
            final.category = "empty_valid"
        return final
def native_protocol_summary() -> dict:
    return {
        "mode": "native_api",
        "signer": "offline_python",
        "offline_algorithm": True,
        "sdk_version": SDK_VERSION,
        "scm_version": SCM_VERSION,
        "x_bogus": BOGUS_VALUE,
        "raw_query_required": True,
    }


__all__ = [
    "TikTokNativeClient", "TikTokNativeResult", "native_protocol_summary",
    "comment_pairs", "comment_reply_pairs", "search_pairs",
]
