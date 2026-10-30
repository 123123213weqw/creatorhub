import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.main  # noqa: F401 - initializes the package import graph used by browser modules
from app.browser.account_hub import (
    _FOLLOW_NAV,
    _click_ks_profile_stat,
    _console_safe,
    _harvest_user_lists,
    _norm_follow_user,
)
from app.browser.ks_fetcher import (
    _dig_comments,
    _extract_rest_profile,
    fetch_ks_search,
)
from app.browser.login import (
    _KS_HOME_URL,
    _click_ks_login_button,
    _ks_web_login_ready,
)
from app.platforms.kuaishou.extract import (
    flatten_ks_comments,
    parse_ks_comment,
    parse_self_user,
)
from app.models import PublishTask
from app.platforms.kuaishou.publish import (
    IMAGE_URL,
    MANAGE_URL,
    VIDEO_URL,
    _published_work_url,
    _upload_files,
)


class _LoginCandidate:
    def __init__(self, visible):
        self.visible = visible
        self.clicked = False

    async def is_visible(self):
        return self.visible

    async def click(self, **_kwargs):
        self.clicked = True


class _LoginLocator:
    def __init__(self, candidates):
        self.candidates = candidates

    async def count(self):
        return len(self.candidates)

    def nth(self, index):
        return self.candidates[index]


class _LoginPage:
    def __init__(self, candidates):
        self.candidates = candidates

    def locator(self, _selector):
        return _LoginLocator(self.candidates)


class _UploadButton(_LoginCandidate):
    pass


class _UploadButtonLocator(_LoginLocator):
    pass


class _Chooser:
    def __init__(self):
        self.files = None

    async def set_files(self, files):
        self.files = files


class _ChooserInfo:
    def __init__(self, chooser):
        self._chooser = chooser

    @property
    def value(self):
        async def get():
            return self._chooser
        return get()


class _ChooserContext:
    def __init__(self, chooser):
        self.info = _ChooserInfo(chooser)

    async def __aenter__(self):
        return self.info

    async def __aexit__(self, *_args):
        return False


class _UploadPage:
    def __init__(self):
        self.button = _UploadButton(True)
        self.chooser = _Chooser()
        self.requested_name = ""

    def expect_file_chooser(self, **_kwargs):
        return _ChooserContext(self.chooser)

    def get_by_role(self, _role, *, name, exact):
        self.requested_name = name
        assert exact is True
        return _UploadButtonLocator([self.button])


class _SearchResponse:
    def __init__(self, url, payload, post_data=""):
        self.url = url
        self.status = 200
        self._payload = payload
        self.request = SimpleNamespace(post_data=post_data)

    async def json(self):
        return self._payload


class _SearchMouse:
    async def wheel(self, *_args):
        return None


class _SearchPage:
    def __init__(self, responses):
        self.responses = responses
        self.url = "about:blank"
        self.mouse = _SearchMouse()
        self.callback = None
        self.closed = False

    def on(self, event, callback):
        assert event == "response"
        self.callback = callback

    async def goto(self, url, **_kwargs):
        self.url = url
        for response in self.responses:
            await self.callback(response)

    async def wait_for_timeout(self, _value):
        return None

    async def evaluate(self, _script):
        return None

    async def close(self):
        self.closed = True


class _Counter:
    def __init__(self, text, visible=True):
        self.text = text
        self.visible = visible
        self.clicked = False

    async def is_visible(self):
        return self.visible

    async def inner_text(self):
        return self.text

    async def click(self, **_kwargs):
        self.clicked = True


class _CounterLocator:
    def __init__(self, counters):
        self.counters = counters

    async def count(self):
        return len(self.counters)

    def nth(self, index):
        return self.counters[index]


class _CounterPage:
    def __init__(self, counters):
        self.counters = counters
        self.selector = ""

    def locator(self, selector):
        self.selector = selector
        return _CounterLocator(self.counters)


class KuaishouLoginTests(unittest.TestCase):
    def test_login_opens_rendered_recommendation_page(self):
        self.assertEqual(_KS_HOME_URL, "https://www.kuaishou.com/new-reco")

    def test_current_webday7_cookie_is_accepted_with_user_id(self):
        self.assertTrue(_ks_web_login_ready({
            "userId", "kuaishou.server.webday7_st",
        }))
        self.assertTrue(_ks_web_login_ready({"passToken"}))
        self.assertFalse(_ks_web_login_ready({"userId"}))

    def test_only_visible_login_button_is_clicked(self):
        hidden = _LoginCandidate(False)
        visible = _LoginCandidate(True)
        clicked = asyncio.run(_click_ks_login_button(
            _LoginPage([hidden, visible]), ("selector",)))
        self.assertTrue(clicked)
        self.assertFalse(hidden.clicked)
        self.assertTrue(visible.clicked)


class KuaishouProfileTests(unittest.TestCase):
    def test_current_flat_profile_response_is_normalized(self):
        raw = {
            "result": 1,
            "eid": "3xPUBLIC",
            "userName": "账号名称",
            "userId": 123456,
            "userDefineId": "KS-NAME",
            "userHead": "https://img.example/avatar.jpg",
            "fans": 23,
            "follows": 7,
            "like": 999,
            "sex": "M",
            "photoCount": 4,
        }
        profile, keys = _extract_rest_profile(raw)
        self.assertIn("eid", keys)
        parsed = parse_self_user(profile)
        self.assertEqual(parsed["nickname"], "账号名称")
        self.assertEqual(parsed["sec_uid"], "3xPUBLIC")
        self.assertEqual(parsed["douyin_id"], "KS-NAME")
        self.assertEqual(parsed["avatar"], "https://img.example/avatar.jpg")
        self.assertEqual(parsed["follower_count"], 23)
        self.assertEqual(parsed["following_count"], 7)
        self.assertEqual(parsed["aweme_count"], 4)
        self.assertEqual(parsed["total_favorited"], 999)
        self.assertEqual(parsed["gender"], "M")


class KuaishouFollowSyncTests(unittest.TestCase):
    def test_navigation_uses_owner_profile_counters_not_sidebar_text(self):
        self.assertEqual(_FOLLOW_NAV["kuaishou"]["open"]["following"],
                         ["ksstat:关注"])
        self.assertEqual(_FOLLOW_NAV["kuaishou"]["open"]["fan"],
                         ["ksstat:粉丝"])

    def test_exact_profile_counter_selects_each_direction(self):
        following = _Counter("关注 3")
        fan = _Counter("粉丝 2")
        page = _CounterPage([following, fan])

        clicked = asyncio.run(_click_ks_profile_stat(page, "粉丝"))

        self.assertTrue(clicked)
        self.assertEqual(page.selector, ".profile-top .count span.isOwner")
        self.assertFalse(following.clicked)
        self.assertTrue(fan.clicked)

    def test_current_relation_payload_is_parsed_without_cross_direction_flags(self):
        payload = {"result": 1, "fols": [{
            "user_id": "3x-user", "user_name": "测试用户",
            "headurl": "https://img.example/user.jpg",
            "user_text": "签名", "isFollowing": True,
        }]}
        found = []
        _harvest_user_lists(payload, found)

        following = _norm_follow_user(found[0], "following")
        fan = _norm_follow_user(found[0], "fan")

        self.assertEqual(following["uid"], "3x-user")
        self.assertTrue(following["is_following"])
        self.assertEqual(fan["uid"], "3x-user")
        self.assertTrue(fan["is_following"])

    def test_diagnostic_with_emoji_is_gbk_printable(self):
        with patch("app.browser.account_hub.sys.stdout",
                   SimpleNamespace(encoding="gbk")):
            safe = _console_safe("昵称🖇️")
        safe.encode("gbk")
        self.assertIn("U0001f587", safe)


class KuaishouCommentTests(unittest.TestCase):
    def test_graphql_root_comments_v2_and_replies_are_parsed(self):
        reply = {
            "commentId": "reply-1", "content": "回复", "authorName": "乙",
            "timestamp": 1_700_000_000_000,
        }
        root = {
            "commentId": "root-1", "content": "评论", "authorName": "甲",
            "timestamp": 1_700_000_000_000, "subCommentsV2": [reply],
        }
        payload = {
            "data": {"visionCommentList": {"rootCommentsV2": [root]}}
        }
        roots = _dig_comments(payload)
        self.assertEqual(roots, [root])
        flattened = flatten_ks_comments(roots)
        self.assertEqual([item["commentId"] for item in flattened],
                         ["root-1", "reply-1"])
        self.assertEqual(parse_ks_comment(flattened[0])["text"], "评论")

    def test_rest_v2_comments_under_data_are_parsed(self):
        root = {"comment_id": "rest-1", "content": "评论", "author_name": "甲",
                "timestamp": 1_700_000_000_000}
        for container in (
            {"data": {"rootCommentsV2": [root]}},
            {"data": {"commentList": [root]}},
            {"data": {"comments": [root]}},
            {"rootCommentsV2": [root]},
        ):
            with self.subTest(container=list(container)):
                self.assertEqual(_dig_comments(container), [root])

    def test_comments_under_unknown_operation_name_fall_back_recursively(self):
        root = {"commentId": "gql-1", "content": "评论", "authorName": "甲",
                "timestamp": 1_700_000_000_000}
        # 操作名/层级都改了,但评论数组仍挂在「comment 命名键」下,兜底也要能挖到。
        payload = {
            "data": {"commentListQueryV3": {"feeds": {"rootCommentsV2": [root]}}}
        }
        self.assertEqual(_dig_comments(payload), [root])

    def test_comments_under_non_comment_key_detected_by_shape(self):
        root = {"commentId": "shape-1", "content": "评论", "authorName": "甲",
                "timestamp": 1_700_000_000_000}
        # 键名里没有 comment(如 feeds/items),靠元素形状(commentId)兜底命中。
        payload = {"data": {"visionCommentList": {"feeds": [root]}}}
        self.assertEqual(_dig_comments(payload), [root])


class KuaishouSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_and_graphql_search_responses_are_deduplicated(self):
        def feed(photo_id):
            return {"photo": {"id": photo_id,
                              "photoUrl": f"https://media.test/{photo_id}.mp4"}}

        responses = [
            _SearchResponse(
                "https://www.kuaishou.com/rest/v/search/feed",
                {"data": {"feeds": [feed("p1"), feed("known")]}}),
            _SearchResponse(
                "https://www.kuaishou.com/graphql",
                {"data": {"visionSearchPhoto": {
                    "feeds": [feed("p1"), feed("p2")]}}},
                '{"operationName":"visionSearchPhoto"}'),
        ]
        page = _SearchPage(responses)
        manager = SimpleNamespace(new_page=AsyncMock(return_value=page))

        values, error = await fetch_ks_search(
            manager, SimpleNamespace(), "露营 装备", {"known"},
            max_scrolls=1, settle_ms=0)

        self.assertEqual(error, "")
        self.assertEqual([item["photo"]["id"] for item in values], ["p1", "p2"])
        self.assertIn("searchKey=%E9%9C%B2%E8%90%A5%20%E8%A3%85%E5%A4%87", page.url)
        self.assertTrue(page.closed)


class KuaishouPublishRouteTests(unittest.TestCase):
    def test_images_and_video_use_the_current_unified_page(self):
        self.assertEqual(IMAGE_URL, VIDEO_URL)
        self.assertTrue(VIDEO_URL.endswith("/article/publish/video"))

    def test_publish_response_photo_id_builds_direct_work_url(self):
        self.assertEqual(_published_work_url({
            "result": 1, "data": {"photoId": "3xNEWPHOTO123"},
        }), "https://www.kuaishou.com/short-video/3xNEWPHOTO123")

    def test_old_publish_form_result_is_normalized_to_manage_page(self):
        task = PublishTask(
            platform="kuaishou", status="done", result_url=VIDEO_URL,
            media_json="[]",
        )
        self.assertEqual(app.main._publish_dict(task)["result_url"], MANAGE_URL)

    def test_image_upload_uses_the_real_upload_button_and_file_chooser(self):
        page = _UploadPage()
        uploaded = asyncio.run(_upload_files(page, "images", ["one.png", "two.jpg"]))
        self.assertTrue(uploaded)
        self.assertEqual(page.requested_name, "上传图片")
        self.assertTrue(page.button.clicked)
        self.assertEqual(page.chooser.files, ["one.png", "two.jpg"])


if __name__ == "__main__":
    unittest.main()
