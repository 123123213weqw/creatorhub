"""Huilima 私聊机器人：飞书长连接 + 可选 DeepSeek；Hub CLI 受控命令；不替代人工发布确认。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

os.umask(0o077)
PRIVATE = Path.home() / ".config/huilima-bot"
LOG = logging.getLogger("huilima")


def input_text(content: str) -> str:
    try:
        value = json.loads(content).get("text", "")
        return value.strip() if isinstance(value, str) else ""
    except (ValueError, AttributeError, TypeError):
        return ""


def response_text(message_type: str, content: str, ai_enabled=False, model="", hub_enabled=False) -> str:
    if message_type != "text":
        return "已收到消息。当前仅支持文字私聊，暂不解析文件、图片或执行业务任务。发送“帮助”查看说明。"
    text = input_text(content)
    if text.lower() in {"帮助", "help", "/help"}:
        if ai_enabled:
            hub_help = ("\nHub CLI 自然语言查询/写操作准备示例：\n• 看看 Hub 总览\n• 查看账号状态\n• 列出失败任务\n• 查看客户朋友圈任务状态\n• 检查备份连接和Hub扩展是否就绪\n• 查看采集任务，或查看采集任务 123 的结果\n"
                        "Hub返回的状态、数量、安全编号和时间会发送给DeepSeek整理；正文、昵称、客户/成员标识、路径、Cookie和密钥不会提供给模型。\n") if hub_enabled else ""
            return ("Huilima Bot · DeepSeek 私聊助手\n"
                    "直接发送文字即可对话，文字和最近三轮短期上下文及少量工具对象引用会发往 DeepSeek 官方 API。\n"
                    "• 状态：检查通道和模型\n• 清空上下文：清除本机内存中的本轮会话\n"
                    "上下文 30 分钟不活跃后过期，服务重启即清空。" + hub_help +
                    "不读取群聊，可生成写操作确认单；精确确认码后可批准发布入队、派发成员朋友圈任务或启动受控采集；不自动确认、不读取群聊、不开放删除/密钥/权限修改。")
        return ("Huilima Bot 私聊测试版\n"
                "• 状态：检查私聊通道\n"
                "• 其他文字：返回收到确认\n"
                "目前尚未接入 AI、内容发布、CRM 或任务执行；不会读取群聊。")
    if text.lower() in {"状态", "status", "/status", "ping"}:
        if ai_enabled:
            capabilities = "已配置按需加载的只读查询及写操作准备工具；需同一私聊精确确认码才执行发布入队/企微成员派发等受控写入。" if hub_enabled else "仅支持文字问答。"
            return f"私聊服务运行中，已配置 DeepSeek（{model}）。{capabilities}不读取群聊；此命令不调用模型或 Hub，不能代表实时 API 健康检查。"
        return "私聊通道运行中。仅处理人与机器人的私聊，尚未接入 AI 或业务系统。"
    return "收到，私聊通道已接通。当前仅做收发测试，未执行任何业务任务。发送“帮助”查看说明。"


class Queue:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS replies (
                message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
                sender_id TEXT NOT NULL, reply TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, created REAL NOT NULL, sent REAL
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(replies)")}
            if "input_text" not in columns:
                db.execute("ALTER TABLE replies ADD COLUMN input_text TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def accept(self, data, allowed_senders=(), ai_config=None, hub_config=None) -> bool:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if not message or not sender:
            return False
        # 即使后续误配群消息权限，这里仍拒绝群聊和机器人消息。
        if message.chat_type != "p2p" or sender.sender_type != "user":
            return False
        sender_id = getattr(getattr(sender, "sender_id", None), "open_id", None)
        if not sender_id or not message.message_id or not message.chat_id:
            return False
        if allowed_senders and sender_id not in allowed_senders:
            return False
        ai_config = ai_config or {}
        enabled = bool(ai_config.get("enabled"))
        text = input_text(message.content) if message.message_type == "text" else ""
        command = text.lower()
        prompt = ""
        reply = response_text(message.message_type, message.content, enabled, ai_config.get("model", ""), bool((hub_config or {}).get("enabled")))
        if (enabled or (hub_config or {}).get("enabled")) and text and command not in {"帮助", "help", "/help", "状态", "status", "/status", "ping"}:
            if len(text) > int(ai_config.get("max_input_chars", 8000)):
                reply = "文字过长，请拆成每条不超过 8000 字符后发送。本条没有发送给 DeepSeek。"
            elif command in {"清空上下文", "/reset"}:
                prompt, reply = "/reset", ""
            else:
                prompt, reply = text, ""
        with self.connect() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO replies(message_id,chat_id,sender_id,reply,created,input_text) VALUES (?,?,?,?,?,?)",
                (message.message_id, message.chat_id, sender_id, reply, time.time(), prompt),
            )
            return bool(result.rowcount)

    def next(self):
        with self.connect() as db:
            return db.execute(
                "SELECT message_id,chat_id,reply,attempts,input_text,sender_id FROM replies "
                "WHERE status='pending' AND next_try<=? ORDER BY created LIMIT 1", (time.time(),)
            ).fetchone()

    def finish(self, message_id, success, attempts):
        with self.connect() as db:
            db.execute(
                "UPDATE replies SET status=?,attempts=?,next_try=?,sent=? WHERE message_id=?",
                ("sent" if success else ("failed" if attempts >= 5 else "pending"), attempts,
                 time.time() + min(120, 2 ** attempts), time.time() if success else None, message_id),
            )
            if success or attempts >= 5:
                db.execute("UPDATE replies SET reply='',input_text='' WHERE message_id=?", (message_id,))

    def prepare(self, message_id, reply):
        with self.connect() as db:
            db.execute("UPDATE replies SET reply=?,input_text='' WHERE message_id=?", (reply, message_id))

    def reserve_ai_call(self, limit):
        day = time.strftime("%Y-%m-%d")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO ai_usage(day,calls) VALUES (?,0)", (day,))
            calls = db.execute("SELECT calls FROM ai_usage WHERE day=?", (day,)).fetchone()[0]
            if calls >= limit:
                return False
            db.execute("UPDATE ai_usage SET calls=calls+1 WHERE day=?", (day,))
            return True

    def counts(self):
        with self.connect() as db:
            return dict(db.execute("SELECT status,COUNT(*) FROM replies GROUP BY status").fetchall())


class Status:
    def __init__(self, path, model="", hub_enabled=False, workflow_enabled=False, model_tool_count=0,model_write_preparations=0):
        self.path = path
        self.connected = False
        self.started = time.time()
        self.lock = threading.Lock()
        self.model = model
        self.hub_enabled = hub_enabled
        self.workflow_enabled = workflow_enabled
        self.model_tool_count = model_tool_count
        self.model_write_preparations=model_write_preparations
        self.counts = {}

    def save(self, counts=None):
        with self.lock:
            if counts is not None:
                self.counts = counts
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({
                "pid": os.getpid(), "started": self.started, "updated": time.time(),
                "connected": self.connected, "mode": "p2p-deepseek-hub-cli" if self.workflow_enabled else "p2p-deepseek-hub-readonly" if self.hub_enabled else ("p2p-deepseek" if self.model else "p2p-test-only"),
                "model": self.model, "model_tool_count": self.model_tool_count, "model_write_tools": 0, "model_write_preparations":self.model_write_preparations,"human_confirmation":"same_chat_exact_code", "tool_contract_version": "0.12-confirmed-actions-v1", "hub_readonly": self.hub_enabled and not self.workflow_enabled, "hub_cli_workflows": self.workflow_enabled, "counts": self.counts,
            }, ensure_ascii=False), encoding="utf8")
            temp.replace(self.path)


class SDKLogFilter(logging.Filter):
    def __init__(self, secrets, status):
        super().__init__()
        self.secrets = [s for s in secrets if s]
        self.status = status

    def filter(self, record):
        message = record.getMessage()
        if "connected to " in message:
            self.status.connected = True
            self.status.save()
        elif any(word in message for word in ("disconnected", "connect failed", "receive message loop exit")):
            self.status.connected = False
            self.status.save()
        # SDK 的 INFO 连接日志带临时鉴权 URL；禁止把它写入本机日志。
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(?:https?|wss?)://\S+", "[URL REDACTED]", message)
        record.msg, record.args = message, ()
        return True


def main():
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    from lark_oapi.core.log import logger as sdk_logger
    from deepseek import DeepSeek, AIError
    from cli_bridge import CLIBridge, HELP

    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = PRIVATE / "config.json"
    if config_path.stat().st_mode & 0o077:
        raise RuntimeError("config.json 权限必须为 600")
    config = json.loads(config_path.read_text())
    if not re.fullmatch(r"cli_[A-Za-z0-9]{8,64}",str(config.get("app_id",""))):
        raise RuntimeError("应用 ID 格式无效，请使用本机私有配置中自己的应用 ID")
    lock = (PRIVATE / "service.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ai_config = config.get("ai", {})
    ai_enabled = bool(ai_config.get("enabled"))
    hub_config = config.get("hub", {})
    hub = CLIBridge(hub_config, config.get("allowed_sender_open_ids", ()), PRIVATE / "hub-audit.jsonl",
                   secrets=[config["app_secret"], ai_config.get("api_key")],actions_path=PRIVATE/"actions.sqlite3") if hub_config.get("enabled") else None
    status = Status(PRIVATE / "status.json", ai_config.get("model", "") if ai_enabled else "", hub is not None, workflow_enabled=hub is not None, model_tool_count=len(hub.tools) if hub else 0,model_write_preparations=hub.write_preparation_count if hub else 0)
    queue = Queue(PRIVATE / "state.sqlite3")
    status.save(queue.counts())
    sdk_logger.addFilter(SDKLogFilter([config["app_secret"], ai_config.get("api_key")], status))
    client = (lark.Client.builder().app_id(config["app_id"]).app_secret(config["app_secret"])
              .timeout(15).log_level(lark.LogLevel.INFO).build())
    ai = DeepSeek(ai_config, reserve_call=lambda: queue.reserve_ai_call(int(ai_config.get("max_calls_per_day", 100))), hub=hub,metrics_callback=lambda metrics: LOG.info("model_metrics %s",json.dumps(metrics,separators=(",",":")))) if ai_enabled else None

    def worker():
        last_status = 0
        while True:
            if time.time() - last_status > 15:
                if ai is not None:
                    ai.expire()
                if hub:hub.actions.expire()
                status.save(queue.counts())
                last_status = time.time()
            row = queue.next()
            if not row:
                time.sleep(0.5)
                continue
            message_id, chat_id, reply, attempts, prompt, actor = row
            if prompt:
                if prompt == "/reset":
                    if ai is not None:
                        ai.forget(chat_id)
                    if hub:hub.actions.cancel(actor,chat_id)
                    reply = "已清空本机内存中的短期上下文。接下来的文字将作为新对话。"
                elif hub is not None and actor not in hub.allowed_senders:
                    reply = "当前发送者未获授权，未调用 Hub 或模型。"
                else:
                    try:
                        if hub:hub.begin_message(message_id,chat_id,actor)
                        handled,reply=hub.handle_confirmation(prompt,actor,chat_id) if hub else (False,"")
                        if not handled:handled,reply=hub.handle_command(prompt, actor, message_id) if hub else (False, "")
                        if not handled and ai is not None:
                            reply = ai.generate(chat_id, prompt, actor=actor)
                        elif not handled:
                            reply = "AI 服务未启用，本条文字未发给模型。显式 Hub 命令仍可使用。"
                        if handled and ai is not None and hub:ai.observe_local(chat_id,actor,hub.last_receipt)
                        LOG.info("request_result success=true handled_locally=%s model_called=%s tool_count=%s request_hash=%s", handled, not handled and ai is not None, len(getattr(ai,"last_tool_names",[])) if not handled and ai else 0, hashlib.sha256(message_id.encode()).hexdigest()[:16])
                    except AIError as error:
                        LOG.warning("ai_result success=false code=%s", error.code)
                        reply = ("今日模型调用已达 100 次上限，请明天再试。" if error.code == "daily_limit" else
                                 f"DeepSeek 暂未返回有效回复（{error.code}），请稍后再试。")
                    except Exception as error:
                        LOG.warning("ai_exception type=%s", type(error).__name__)
                        reply = "模型处理暂时失败，请稍后再试。"
                # 先保存生成结果并清除原始输入，飞书发送失败只重发，不重复调用模型。
                queue.prepare(message_id, reply)
            # 飞书 uuid 幂等键与持久去重共同避免重推/重启造成重复回复。
            uuid = hashlib.sha256(message_id.encode()).hexdigest()[:40]
            request = (CreateMessageRequest.builder().receive_id_type("chat_id")
                       .request_body(CreateMessageRequestBody.builder().receive_id(chat_id)
                                     .msg_type("text").content(json.dumps({"text": reply}, ensure_ascii=False))
                                     .uuid(uuid).build()).build())
            try:
                result = client.im.v1.message.create(request)
                success = result.success()
                LOG.info("send_result success=%s code=%s", success, result.code)
            except Exception as error:
                # 不记录异常正文，防止第三方错误消息包含 URL、令牌或内容。
                LOG.warning("send_exception type=%s", type(error).__name__)
                success = False
            queue.finish(message_id, success, attempts + 1)
            status.save(queue.counts())

    def receive(data):
        accepted=queue.accept(data, config.get("allowed_sender_open_ids", ()), ai_config, hub_config)
        if accepted and hub:
            text=input_text(getattr(data.event.message,"content",""))
            if text.lower() in {"帮助","help","/help"}:
                queue.prepare(data.event.message.message_id, HELP+"\n\n飞书仍只处理白名单私聊；问答文字会发送到 DeepSeek 官方 API。")

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(receive).build())
    threading.Thread(target=worker, daemon=True).start()
    LOG.info("starting p2p-only bot; ai_enabled=%s; hub_cli_enabled=%s; no message bodies are logged", ai_enabled, hub is not None)
    ws = lark.ws.Client(config["app_id"], config["app_secret"],
                        event_handler=handler, log_level=lark.LogLevel.INFO)
    ws.start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
