from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest

from bot import Queue, response_text, SDKLogFilter, Status
from deepseek import DeepSeek, AIError
import httpx
import json
import logging
from unittest.mock import patch


def event(chat_type="p2p", sender_type="user", message_id="test-id", sender_id="ou_test"):
    return NS(event=NS(
        sender=NS(sender_type=sender_type, sender_id=NS(open_id=sender_id)),
        message=NS(chat_type=chat_type, message_id=message_id, chat_id="oc_test",
                   message_type="text", content='{"text":"hello"}'),
    ))


class BotTest(unittest.TestCase):
    def test_hub_command_queues_even_when_cloud_ai_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Queue(Path(tmp) / "state.db")
            data = event()
            data.event.message.content = json.dumps({"text": "/账号"})
            self.assertTrue(q.accept(data, ["ou_test"], {"enabled": False}, {"enabled": True}))
            self.assertEqual(q.next()["input_text"], "/账号")

    def test_no_group_or_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Queue(Path(tmp) / "state.db")
            self.assertFalse(q.accept(event(chat_type="group")))
            self.assertFalse(q.accept(event(sender_type="app")))
            self.assertIsNone(q.next())

    def test_persisted_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            q = Queue(path)
            self.assertTrue(q.accept(event()))
            self.assertFalse(Queue(path).accept(event()))
            q.finish("test-id", True, 1)
            self.assertIsNone(q.next())
            self.assertEqual(q.counts(), {"sent": 1})

    def test_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Queue(Path(tmp) / "state.db")
            self.assertFalse(q.accept(event(), ["ou_other"]))
            self.assertTrue(q.accept(event(), ["ou_test"]))

    def test_retry_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Queue(Path(tmp) / "state.db")
            q.accept(event())
            q.finish("test-id", False, 5)
            self.assertIsNone(q.next())
            self.assertEqual(q.counts(), {"failed": 1})

    def test_help_and_no_echo(self):
        self.assertIn("尚未接入 AI", response_text("text", '{"text":"帮助"}'))
        self.assertNotIn("秘密内容", response_text("text", '{"text":"秘密内容"}'))
        self.assertIn("暂不解析", response_text("image", "{}"))
        self.assertIn("收到", response_text("text", "[invalid]"))

    def test_ai_queue_and_prompt_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Queue(Path(tmp) / "state.db")
            self.assertTrue(q.accept(event(), ai_config={"enabled": True}))
            row = q.next()
            self.assertEqual(row["input_text"], "hello")
            q.prepare("test-id", "AI answer")
            self.assertEqual(q.next()["input_text"], "")
            self.assertEqual(q.next()["reply"], "AI answer")
            q.finish("test-id", True, 1)
            with q.connect() as db:
                row = db.execute("SELECT input_text,reply FROM replies").fetchone()
                self.assertEqual(tuple(row), ("", ""))

    def test_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Queue(Path(tmp) / "state.db")
            self.assertTrue(q.reserve_ai_call(1))
            self.assertFalse(q.reserve_ai_call(1))

    def test_commands_and_oversized_input_are_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Queue(Path(tmp) / "state.db")
            for i, text in enumerate(["帮助", "状态", "x" * 8001]):
                data = event(message_id=str(i))
                data.event.message.content = json.dumps({"text": text})
                q.accept(data, ai_config={"enabled": True})
            with q.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM replies WHERE input_text!=''").fetchone()[0], 0)

    def test_sdk_log_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = Status(Path(tmp) / "status.json")
            record = logging.LogRecord("Lark", 20, "", 0, "connected to wss://example.test/?key=abc secret-key", (), None)
            SDKLogFilter(["secret-key"], status).filter(record)
            self.assertNotIn("secret-key", record.getMessage())
            self.assertNotIn("wss://", record.getMessage())
            self.assertTrue(status.connected)


class DeepSeekTest(unittest.TestCase):
    CONFIG = {"api_key": "synthetic-test-key", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"}

    def make(self, handler, reserve=lambda: True):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return DeepSeek(self.CONFIG, reserve, client)

    def test_success_and_context_isolation(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "测试回复"}}]})
        ai = self.make(handler)
        self.assertEqual(ai.generate("chat-a", "你好"), "测试回复")
        ai.generate("chat-a", "继续")
        self.assertEqual(len(requests[-1]["messages"]), 4)
        ai.generate("chat-b", "新对话")
        self.assertEqual(len(requests[-1]["messages"]), 2)
        ai.forget("chat-a")
        ai.generate("chat-a", "重置后")
        self.assertEqual(len(requests[-1]["messages"]), 2)
        self.assertEqual(requests[-1]["thinking"], {"type": "disabled"})

    def test_expired_context(self):
        ai = self.make(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
        ai.generate("chat-a", "old")
        ai.history["chat-a"] = (0, ai.history["chat-a"][1])
        ai.generate("chat-a", "new")
        self.assertEqual(len(ai.history["chat-a"][1]), 2)

    def test_retry_and_hard_failure(self):
        calls = []
        def handler(request):
            calls.append(1)
            return httpx.Response(503 if len(calls) == 1 else 200,
                                  json={"choices": [{"message": {"content": "ok"}}]})
        ai = self.make(handler)
        with patch("deepseek.time.sleep"):
            self.assertEqual(ai.generate("a", "test"), "ok")
        self.assertEqual(len(calls), 2)
        ai = self.make(lambda r: httpx.Response(401, json={"error": "do not log raw error"}))
        with self.assertRaises(AIError) as cm:
            ai.generate("a", "test")
        self.assertEqual(str(cm.exception), "http_401")

    def test_quota_prevents_request(self):
        ai = self.make(lambda r: self.fail("quota must prevent request"), reserve=lambda: False)
        with self.assertRaises(AIError) as cm:
            ai.generate("a", "test")
        self.assertEqual(cm.exception.code, "daily_limit")

    def test_rejects_untrusted_endpoint(self):
        with self.assertRaises(ValueError):
            DeepSeek({**self.CONFIG, "base_url": "https://untrusted.example"})


if __name__ == "__main__":
    unittest.main()
