"""CreatorHub 四个只读工具：固定路由、独立身份校验、字段白名单。无 HTTP 监听器。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import httpx

PLATFORMS = ["all", "douyin", "xhs", "kuaishou", "shipinhao"]
STATES = ["active", "pending", "running", "blocked", "failed", "completed", "all"]


def schema(name, description, properties):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "additionalProperties": False},
    }}


PLATFORM = {"type": "string", "enum": PLATFORMS, "description": "all 表示全部平台，默认 all"}
PAGE = {"type": "integer", "minimum": 1, "maximum": 10000, "description": "页码，默认 1"}
SIZE = {"type": "integer", "minimum": 1, "maximum": 20, "description": "每页数量，默认 10，最多 20"}
TOOLS = [
    schema("hub_overview", "查询 Hub 当前实时总览；统计账号、监控、下载、评论数量。只读。", {"platform": PLATFORM}),
    schema("hub_accounts", "查看 Hub 账号列表和登录状态；不返回 Cookie、代理、浏览器路径。只读。", {
        "platform": PLATFORM, "page": PAGE, "page_size": SIZE}),
    schema("hub_tasks", "查看 Hub 的采集、发布、下载等任务和失败原因。只读，不会执行或重试任务。", {
        "platform": PLATFORM, "state": {"type": "string", "enum": STATES},
        "page": PAGE, "page_size": SIZE}),
    schema("hub_collection_results", "查看已有采集任务/结果。没有 job_id 时先列最近的任务；有 job_id 时分页查看该任务的作品。不会启动采集。", {
        "job_id": {"type": "integer", "minimum": 1}, "platform": PLATFORM,
        "page": PAGE, "page_size": SIZE}),
]


class HubError(Exception):
    pass


class HubTools:
    def __init__(self, config, allowed_senders, audit_path: Path | None = None, http=None, secrets=()):
        base = config["base_url"].rstrip("/")
        u = urlsplit(base)
        if (u.scheme != "http" or u.hostname not in {"127.0.0.1", "localhost", "::1"}
                or u.username or u.password or u.path or u.query or u.fragment or not u.port):
            raise ValueError("Hub 必须是配置指定的本机回环 HTTP 地址，不能使用任意 URL")
        if not allowed_senders:
            raise ValueError("开启 Hub 工具必须配置明确的飞书发送者白名单")
        self.base = base
        self.label = str(config.get("label", "CreatorHub"))[:80]
        self.allowed_senders = frozenset(allowed_senders)
        self.secrets = [s for s in secrets if s]
        self.http = http or httpx.Client(timeout=httpx.Timeout(12, connect=3), follow_redirects=False, trust_env=False)
        self.audit = None
        if audit_path:
            audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.audit = logging.Logger("hub-audit-" + str(id(self)), logging.INFO)
            handler = RotatingFileHandler(audit_path, maxBytes=1024 * 1024, backupCount=2, encoding="utf8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.audit.addHandler(handler)

    def clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[密钥已隐藏]")
            value = re.sub(r"https?://\S+", "[链接已隐藏]", value)
            value = re.sub(r"/(?:Users|home|tmp|var|data)/\S+", "[路径已隐藏]", value)
            value = re.sub(r"(?i)(?:bearer\s+\S+|sk-[a-z0-9_-]{16,})", "[凭证已隐藏]", value)
            value = re.sub(r"(?i)\b(?:cookie|authorization|[a-z_]*token|[a-z_]*secret|api[_-]?key|password)\s*[:=]\s*\S+", "[凭证字段已隐藏]", value)
            return value[:220]
        if value is None or type(value) in {bool, int, float}:
            return value
        if isinstance(value, list):
            return [self.clean(x) for x in value[:10] if isinstance(x, (str, int, float, bool))]
        return None

    def pick(self, row, fields):
        if not isinstance(row, dict):
            raise HubError("unexpected_response")
        return {k: self.clean(row[k]) for k in fields if k in row}

    def get(self, path, params=None):
        # 所有 path 都来自下方硬编码模板，调用者/模型不能传入 URL 或 HTTP 方法。
        for attempt in range(2):
            try:
                response = self.http.get(self.base + path, params=params)
                if response.status_code != 200:
                    if response.status_code in {502, 503, 504} and attempt == 0:
                        time.sleep(.3)
                        continue
                    raise HubError("not_found" if response.status_code == 404 else "hub_http_" + str(response.status_code))
                if len(response.content) > 2 * 1024 * 1024:
                    raise HubError("response_too_large")
                try:
                    return response.json()
                except ValueError:
                    raise HubError("unexpected_response") from None
            except httpx.TransportError:
                if attempt:
                    raise HubError("hub_unavailable") from None
                time.sleep(.3)
        raise HubError("hub_unavailable")

    def validate(self, name, arguments):
        spec = next((x["function"] for x in TOOLS if x["function"]["name"] == name), None)
        if not spec:
            raise HubError("tool_not_allowed")
        if not isinstance(arguments, dict) or set(arguments) - set(spec["parameters"]["properties"]):
            raise HubError("invalid_arguments")
        for key, value in arguments.items():
            p = spec["parameters"]["properties"][key]
            if p["type"] == "integer":
                if type(value) is not int or value < p.get("minimum", 1) or value > p.get("maximum", 2**63 - 1):
                    raise HubError("invalid_arguments")
            elif not isinstance(value, str) or value not in p.get("enum", []):
                raise HubError("invalid_arguments")
        return arguments

    def execute(self, name, arguments, actor=""):
        start = time.monotonic()
        result = {"ok": False, "tool": name if name in {x["function"]["name"] for x in TOOLS} else "unknown",
                  "source": self.label, "fetched_at": datetime.now(timezone.utc).isoformat()}
        try:
            if actor not in self.allowed_senders:
                raise HubError("not_authorized")
            args = self.validate(name, arguments)
            platform = args.get("platform", "all")
            params = {"platform": platform} if platform != "all" else {}
            page, size = args.get("page", 1), args.get("page_size", 10)
            if name == "hub_overview":
                platforms = PLATFORMS[1:] if platform == "all" else [platform]
                items = [self.pick(self.get("/api/overview/summary", {"platform": p}),
                                   ["platform", "accounts", "monitors", "downloaded", "comments"]) for p in platforms]
                data = {"platforms": items, "totals": {
                    k: sum(int(row.get(k) or 0) for row in items) for k in ["accounts", "monitors", "downloaded", "comments"]}}
            elif name == "hub_accounts":
                rows = self.get("/api/accounts", params)
                if not isinstance(rows, list):
                    raise HubError("unexpected_response")
                data = {"total": len(rows), "page": page, "page_size": size,
                        "pages": max(1, (len(rows) + size - 1) // size),
                        "items": [self.pick(row, ["id", "platform", "nickname", "status", "has_read_login", "has_creator", "follower_count"])
                                  for row in rows[(page - 1) * size:page * size]]}
            elif name == "hub_tasks":
                raw = self.get("/api/task-queue", {**params, "state": args.get("state", "active"), "page": page, "page_size": size})
                data = self.pick(raw, ["total", "page", "page_size", "pages", "generated_at"])
                data["items"] = [self.pick(row, ["key", "id", "queue_type", "queue_label", "platform", "account_id", "account_name", "title", "state", "status", "blocked_reason", "error", "created_at", "scheduled_at"])
                                 for row in raw.get("items", [])[:size]]
                if isinstance(raw.get("summary"), dict):
                    data["summary"] = self.pick(raw["summary"], ["total", "active", "pending", "running", "blocked", "failed", "completed"])
            elif "job_id" not in args:
                rows = self.get("/api/collections", {**params, "limit": 100})
                if not isinstance(rows, list):
                    raise HubError("unexpected_response")
                data = {"view": "recent_jobs", "snapshot_limit": 100, "total_in_snapshot": len(rows),
                        "more_may_exist": len(rows) >= 100, "page": page, "page_size": size,
                        "items": [self.pick(row, ["id", "platform", "account_id", "source_type", "source_name", "keywords", "status", "progress_percent", "content_count", "comment_count", "created_at"])
                                  for row in rows[(page - 1) * size:page * size]]}
            else:
                # platform 不能替代任务归属检查，详情始终先读取任务元数据确认平台。
                job = self.get(f"/api/collections/{args['job_id']}")
                if platform != "all" and job.get("platform") != platform:
                    raise HubError("platform_mismatch")
                raw = self.get(f"/api/collections/{args['job_id']}/contents", {"page": page, "page_size": size})
                data = {**self.pick(raw, ["total", "page", "page_size", "pages"]), "view": "contents", "job_id": args["job_id"],
                        "job": self.pick(job, ["id", "platform", "status", "content_count", "comment_count"]),
                        "items": [self.pick(row, ["id", "job_id", "platform", "keyword", "aweme_id", "desc", "author_name", "media_type", "create_time", "like_count", "comment_count", "collected_comment_count", "download_status"])
                                  for row in raw.get("items", [])[:size]]}
            result.update(ok=True, data=data)
        except HubError as error:
            result.update(error=str(error), message="查询未成功，请按错误状态重试或检查 Hub；不要将失败解释为数据为零。")
        except (KeyError, TypeError, ValueError, AttributeError):
            result.update(error="unexpected_response", message="Hub 响应格式不符合预期，无法确认数据。")
        if self.audit:
            self.audit.info(json.dumps({"time": result["fetched_at"], "actor_hash": hashlib.sha256(actor.encode()).hexdigest()[:12],
                "tool": result["tool"], "ok": result["ok"], "error": result.get("error"),
                "duration_ms": round((time.monotonic() - start) * 1000)}, ensure_ascii=False))
        return result
