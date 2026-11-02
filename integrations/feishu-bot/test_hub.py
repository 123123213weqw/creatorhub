import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from deepseek import AIError, DeepSeek
from hub import HubTools, TOOLS


class HubTest(unittest.TestCase):
    def make(self, handler):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        return HubTools({"base_url": "http://127.0.0.1:18080", "label": "test"}, ["owner"], http=http,
                        secrets=["real-test-secret"])

    def test_accounts_redaction_and_pagination(self):
        requests = []
        def handler(r):
            requests.append(r)
            return httpx.Response(200, json=[{
                "id": i, "nickname": "real-test-secret", "platform": "douyin", "has_read_login": True,
                "cookie": "never-export-cookie", "profile_dir": "/home/private", "proxy": "private-proxy"
            } for i in range(25)])
        hub = self.make(handler)
        result = hub.execute("hub_accounts", {"page": 2, "page_size": 10}, actor="owner")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["items"]), 10)
        self.assertEqual(result["data"]["items"][0]["id"], 10)
        self.assertEqual(result["data"]["total"], 25)
        text = json.dumps(result)
        for secret in ["real-test-secret", "never-export-cookie", "/home/private", "private-proxy"]:
            self.assertNotIn(secret, text)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].url.path, "/api/accounts")

    def test_four_platform_overview(self):
        requests = []
        def handler(r):
            requests.append(r)
            return httpx.Response(200, json={"platform": r.url.params["platform"], "accounts": 1, "monitors": 2})
        result = self.make(handler).execute("hub_overview", {}, actor="owner")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["totals"]["accounts"], 4)
        self.assertEqual(len(requests), 4)

    def test_identity_and_write_rejection(self):
        hub = self.make(lambda r: self.fail("denied calls must not reach HTTP"))
        for name, args, actor, error in [
            ("hub_accounts", {}, "other", "not_authorized"),
            ("hub_publish", {}, "owner", "tool_not_allowed"),
            ("hub_accounts", {"url": "http://untrusted.example"}, "owner", "invalid_arguments"),
            ("hub_accounts", {"actor": "owner"}, "owner", "invalid_arguments"),
            ("hub_collection_results", {"job_id": "../../etc/passwd"}, "owner", "invalid_arguments"),
            ("hub_collection_results", {"job_id": True}, "owner", "invalid_arguments"),
            ("hub_accounts", {"page_size": 21}, "owner", "invalid_arguments"),
            ("hub_overview", {"platform": "unknown"}, "owner", "invalid_arguments"),
        ]:
            with self.subTest(error=error, args=args):
                self.assertEqual(hub.execute(name, args, actor=actor)["error"], error)

    def test_config_boundary(self):
        for url in ["http://example.com:18080", "https://127.0.0.1:18080", "http://user:pass@localhost:80", "http://localhost:80/path"]:
            with self.assertRaises(ValueError):
                HubTools({"base_url": url}, ["owner"])
        with self.assertRaises(ValueError):
            HubTools({"base_url": "http://127.0.0.1:18080"}, [])

    def test_tasks_shape_and_filter(self):
        def handler(r):
            self.assertEqual(r.url.params["state"], "failed")
            return httpx.Response(200, json={"items": [{"id": 1, "status": "failed", "error": "see http://private/path?token=secret /home/private/file"}],
                                          "total": 1, "summary": {"failed": 1, "secret": "hidden"}})
        result = self.make(handler).execute("hub_tasks", {"state": "failed"}, actor="owner")
        self.assertTrue(result["ok"])
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(result["data"]["summary"], {"failed": 1})

    def test_collection_list_and_result(self):
        requests = []
        def handler(r):
            requests.append(r)
            if r.url.path == "/api/collections":
                return httpx.Response(200, json=[{"id": 9, "platform": "xhs", "content_count": 1, "download_dir": "/private"}])
            if r.url.path == "/api/collections/9":
                return httpx.Response(200, json={"id": 9, "platform": "xhs", "content_count": 1})
            return httpx.Response(200, json={"total": 1, "items": [{"id": 3, "desc": "测试作品", "xsec_token": "never-export-xsec", "url": "http://private/"}]})
        hub = self.make(handler)
        self.assertTrue(hub.execute("hub_collection_results", {}, actor="owner")["ok"])
        result = hub.execute("hub_collection_results", {"job_id": 9}, actor="owner")
        self.assertEqual(result["data"]["items"][0]["desc"], "测试作品")
        self.assertNotIn("never-export", json.dumps(result))
        self.assertEqual(hub.execute("hub_collection_results", {"job_id": 9, "platform": "douyin"}, actor="owner")["error"], "platform_mismatch")
        self.assertTrue(all(r.method == "GET" for r in requests))

    def test_failure_not_empty_success(self):
        hub = self.make(lambda r: httpx.Response(404, json={"detail": "secret internal data"}))
        result = hub.execute("hub_tasks", {}, actor="owner")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_found")
        self.assertNotIn("secret internal", json.dumps(result))

    def test_audit_has_no_inputs_or_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            hub = self.make(lambda r: httpx.Response(200, json=[]))
            audited = HubTools({"base_url": "http://127.0.0.1:18080"}, ["owner"], path, http=hub.http)
            audited.execute("hub_accounts", {}, actor="owner")
            row = json.loads(path.read_text())
            self.assertTrue(row["ok"])
            self.assertNotIn('"owner"', path.read_text())
            self.assertNotIn("data", row)
            for handler in audited.audit.handlers:
                handler.close()


class ToolLoopTest(unittest.TestCase):
    def test_model_to_hub_to_model(self):
        api_requests, hub_requests = [], []
        def hub_handler(r):
            hub_requests.append(r)
            return httpx.Response(200, json=[{"id": 1, "nickname": "测试号", "cookie": "never-export"}])
        hub_http = httpx.Client(transport=httpx.MockTransport(hub_handler))
        self.addCleanup(hub_http.close)
        hub = HubTools({"base_url": "http://localhost:18080"}, ["owner"], http=hub_http)
        def api_handler(r):
            payload = json.loads(r.content)
            api_requests.append(payload)
            if len(api_requests) == 1:
                return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "call_1", "type": "function", "function": {"name": "hub_accounts", "arguments": "{}"}
                }]}}]})
            self.assertEqual(payload["messages"][-1]["role"], "tool")
            self.assertNotIn("never-export", json.dumps(payload))
            return httpx.Response(200, json={"choices": [{"message": {"content": "目前有 1 个账号：测试号。"}}]})
        api_http = httpx.Client(transport=httpx.MockTransport(api_handler))
        self.addCleanup(api_http.close)
        ai = DeepSeek({"base_url": "https://api.deepseek.com", "api_key": "test", "model": "deepseek-flash"}, http=api_http, hub=hub)
        answer = ai.generate("chat", "查看账号", actor="owner")
        self.assertIn("来源：", answer)
        self.assertEqual(len(hub_requests), 1)
        self.assertEqual(len(api_requests[0]["tools"]), 4)

    def test_failed_tools_cannot_be_reported_as_zero(self):
        class FailedHub:
            def execute(self, *args, **kwargs):
                return {"ok": False, "error": "hub_unavailable"}
        calls = []
        def handler(r):
            calls.append(1)
            msg = {"content": "账号数量为零"} if len(calls) > 1 else {
                "tool_calls": [{"id": "c1", "function": {"name": "hub_accounts", "arguments": "{}"}}]}
            return httpx.Response(200, json={"choices": [{"message": msg}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            ai = DeepSeek({"base_url": "https://api.deepseek.com", "api_key": "test", "model": "deepseek-flash"}, http=http, hub=FailedHub())
            answer = ai.generate("a", "查账号", actor="owner")
            self.assertIn("查询未成功", answer)
            self.assertNotIn("账号数量为零", answer)

    def test_tool_loop_has_hard_limit(self):
        class NoHttpHub:
            def execute(self, *args, **kwargs):
                return {"ok": False, "error": "tool_not_allowed"}
        def handler(r):
            return httpx.Response(200, json={"choices": [{"message": {
                "tool_calls": [{"id": "c1", "function": {"name": "publish", "arguments": "{}"}}]
            }}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            ai = DeepSeek({"base_url": "https://api.deepseek.com", "api_key": "test", "model": "deepseek-flash"}, http=http, hub=NoHttpHub())
            with self.assertRaises(AIError) as cm:
                ai.generate("a", "test", actor="owner")
            self.assertEqual(cm.exception.code, "tool_limit")


if __name__ == "__main__":
    unittest.main()
