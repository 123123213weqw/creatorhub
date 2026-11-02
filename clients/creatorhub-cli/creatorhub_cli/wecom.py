"""WeCom summaries via the same local Hub; no credentials or direct Tencent I/O."""
from .client import Error, REQUEST_KEY, field, require

CATALOG = {
    "wecom.status": ({}, []),
    "wecom.request": ({"request_key": REQUEST_KEY}, ["request_key"]),
    "wecom.refresh": ({"request_key": REQUEST_KEY, "acknowledge_customer_ids": field("boolean")},
                      ["request_key", "acknowledge_customer_ids"]),
}


def _attempt(value):
    from .client import pick
    if value is None:
        return None
    out = pick(value, ["status", "started_at", "finished_at", "error_code", "api_code"])
    require(out.get("status") in {"running", "interrupted", "completed", "failed"},
            "invalid_response", "企微刷新状态不正确", 5)
    if "counts" in value:
        c = value["counts"]
        require(type(c) is dict and all(type(c.get(k)) is int and c[k] >= 0
                for k in ("visible_member_count", "enabled_member_count", "customer_count")),
                "invalid_response", "企微摘要数量不正确，不能解释为零", 5)
        require(c["visible_member_count"] == c["enabled_member_count"] == 1,
                "invalid_response", "企微摘要超出单一测试成员范围", 5)
        out["counts"] = {k: c[k] for k in ("visible_member_count", "enabled_member_count", "customer_count")}
    return out


def call(client, command, args):
    from .client import pick
    if command == "wecom.refresh":
        require(args["acknowledge_customer_ids"] is True, "wecom_acknowledgement_required",
                "刷新需显式确认读取测试成员客户ID列表，仅保存数量", 3)
        raw = client._post("/api/integrations/wecom/refresh", {"acknowledge_customer_ids": True}, args["request_key"])
    else:
        path = "status" if command == "wecom.status" else "requests/" + args["request_key"]
        try:
            raw = client._get("/api/integrations/wecom/" + path)
        except Error as error:
            if command == "wecom.status" and error.code == "hub_http_404":
                raise Error("wecom_extension_unavailable", "当前 Hub 尚未部署企微只读扩展；不会改为直连腾讯或启动其他服务", 5) from None
            raise
    require(type(raw) is dict and raw.get("protocol") == "1.0"
            and raw.get("source") == "existing_hub_summary_cache"
            and raw.get("automatic_polling_enabled") is False
            and raw.get("external_write_triggered") is False
            and raw.get("raw_customer_ids_saved") is False,
            "invalid_response", "企微只读接口契约不正确", 5)
    result = pick(raw, ["protocol", "source", "cache_present", "cache_fresh", "cache_age_seconds",
        "clock_skew_detected", "next_allowed_at", "refresh_min_interval_seconds", "cache_ttl_seconds",
        "automatic_polling_enabled", "platform_refresh_triggered", "customer_details_available",
        "chat_content_available", "raw_customer_ids_saved", "external_write_triggered", "replayed"])
    for name in ("last_success", "last_attempt", "refresh"):
        if name in raw:
            result[name] = _attempt(raw[name])
    if command == "wecom.refresh":
        require(result.get("refresh") is not None, "invalid_response", "企微刷新缺少回执", 5)
        if result["refresh"]["status"] in {"failed", "interrupted"}:
            raise Error("wecom_refresh_failed", "刷新未成功；用 wecom status 或原编号 wecom request 核对，旧缓存未作为新数据", 5)
    return {**result, "state_authority": "existing_creatorhub"}
