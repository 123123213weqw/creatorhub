"""Manual WeCom count snapshots in the existing Hub DB, with durable receipts.

No scheduler, customer details, raw IDs, chat content or platform writes.
External I/O happens only after a short, committed SQLite claim transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from .hub_adapter import get_session
from .hub_adapter import AppSetting
from .wecom_readonly import WeComError, load_credentials
from .wecom_customer_readonly import WeComCustomerReadOnly

router = APIRouter(prefix="/api/integrations/wecom")
STATE = "wecom:readonly:v1"
RECEIPT = "wecom:readonly:request:"
MIN_INTERVAL = 900
CACHE_TTL = 3600
KEY_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}"


def _clock():
    return time.time()


def _iso(stamp):
    if (type(stamp) not in {int, float} or not math.isfinite(stamp)
            or not 0 <= stamp <= 253402300799):
        raise HTTPException(409, "wecom_cache_invalid")
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _factory():
    return WeComCustomerReadOnly(load_credentials())


def _read(session, key, default=None):
    row = session.get(AppSetting, key)
    if row is None:
        return default
    try:
        value = json.loads(row.value)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise HTTPException(409, "wecom_cache_invalid") from None


def _write(session, key, value):
    row = session.get(AppSetting, key) or AppSetting(key=key)
    row.value = json.dumps(value, ensure_ascii=False, allow_nan=False)
    session.add(row)


def _state(session):
    state = _read(session, STATE, {
        "next_allowed": 0, "claim": None, "last_attempt": None, "last_success": None,
    })
    stamp = state.get("next_allowed")
    if (set(state) != {"next_allowed", "claim", "last_attempt", "last_success"}
            or type(stamp) not in {int, float} or not math.isfinite(stamp) or not 0 <= stamp <= 253402300799
            or not isinstance(state.get("claim"), (str, type(None)))
            or any(not isinstance(state.get(k), (dict, type(None)))
                   for k in ("last_attempt", "last_success"))):
        raise HTTPException(409, "wecom_cache_invalid")
    if state["claim"] is not None and not re.fullmatch(r"[a-f0-9]{64}", state["claim"]):
        raise HTTPException(409, "wecom_cache_invalid")
    return state


def _counts(report):
    # Project an explicit schema before anything is persisted or returned.
    if (not isinstance(report, dict) or report.get("status") != "ok"
            or report.get("application_enabled") is not True
            or type(report.get("visible_member_count")) is not int
            or report["visible_member_count"] != 1
            or type(report.get("enabled_member_count")) is not int
            or report["enabled_member_count"] != 1
            or report.get("customer_members_api_verified") is not True
            or report.get("customer_list_api_verified") is not True
            or type(report.get("customer_ids_returned")) is not int
            or not 0 <= report["customer_ids_returned"] <= 10000
            or report.get("customer_details_read") is not False
            or report.get("chat_content_read") is not False
            or report.get("raw_customer_ids_saved") is not False
            or any(type(report.get(k)) is not int or report[k] != 0
                   for k in ("messages_sent", "distribution_tasks_created", "moments_tasks_created"))):
        raise WeComError("wecom_summary_contract_invalid")
    return {"visible_member_count": 1, "enabled_member_count": 1,
            "customer_count": report["customer_ids_returned"]}


def _attempt(record, now):
    if record is None:
        return None
    started = record.get("started")
    if (type(started) not in {int, float} or not math.isfinite(started)
            or record.get("status") not in {"running", "completed", "failed"}):
        raise HTTPException(409, "wecom_cache_invalid")
    out = {"status": record["status"], "started_at": _iso(started)}
    if out["status"] == "running" and now - started >= MIN_INTERVAL:
        out["status"] = "interrupted"
    if record.get("finished") is not None:
        out["finished_at"] = _iso(record["finished"])
    if out["status"] in {"completed", "failed"} and "finished_at" not in out:
        raise HTTPException(409, "wecom_cache_invalid")
    if record.get("error_code"):
        code = record["error_code"]
        out["error_code"] = code if isinstance(code, str) and re.fullmatch(r"wecom_[a-z0-9_]{1,80}", code) else "wecom_probe_failed"
    if type(record.get("api_code")) is int:
        out["api_code"] = record["api_code"]
    if record.get("counts") is not None:
        c = record["counts"]
        if (not isinstance(c, dict) or set(c) != {"visible_member_count", "enabled_member_count", "customer_count"}
                or any(type(v) is not int or not 0 <= v <= 10000 for v in c.values())
                or c["visible_member_count"] != 1 or c["enabled_member_count"] != 1):
            raise HTTPException(409, "wecom_cache_invalid")
        out["counts"] = dict(c)
    return out


def _view(state, now):
    last = _attempt(state["last_success"], now)
    attempt = _attempt(state["last_attempt"], now)
    if last is not None and (last["status"] != "completed" or "counts" not in last):
        raise HTTPException(409, "wecom_cache_invalid")
    age = None if last is None else now - state["last_success"]["finished"]
    return {
        "protocol": "1.0", "source": "existing_hub_summary_cache",
        "cache_present": last is not None, "cache_fresh": age is not None and 0 <= age < CACHE_TTL,
        "cache_age_seconds": None if age is None else max(0, int(age)),
        "clock_skew_detected": age is not None and age < 0,
        "last_success": last, "last_attempt": attempt,
        "next_allowed_at": _iso(state["next_allowed"]) if state["next_allowed"] else None,
        "refresh_min_interval_seconds": MIN_INTERVAL, "cache_ttl_seconds": CACHE_TTL,
        "automatic_polling_enabled": False, "platform_refresh_triggered": False,
        "customer_details_available": False, "chat_content_available": False,
        "raw_customer_ids_saved": False, "external_write_triggered": False,
    }


def snapshot():
    with get_session() as session:
        return _view(_state(session), _clock())


def _digest(key):
    if not isinstance(key, str) or not re.fullmatch(KEY_PATTERN, key):
        raise HTTPException(422, "wecom_request_key_required")
    return hashlib.sha256(key.encode()).hexdigest()


def _receipt_view(state, receipt, now, *, replayed, triggered=False):
    return {**_view(state, now), "refresh": _attempt(receipt, now),
            "replayed": replayed, "platform_refresh_triggered": triggered}


def request_status(key):
    digest = _digest(key)
    with get_session() as session:
        receipt = _read(session, RECEIPT + digest)
        if receipt is None:
            raise HTTPException(404, "wecom_receipt_not_found")
        return _receipt_view(_state(session), receipt, _clock(), replayed=True)


def refresh(key):
    digest, now = _digest(key), _clock()
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        state = _state(session)
        old = _read(session, RECEIPT + digest)
        if old is not None:
            return _receipt_view(state, old, now, replayed=True)
        if now < state["next_allowed"]:
            raise HTTPException(429, "wecom_refresh_cooldown")
        receipt = {"status": "running", "started": now}
        state.update(claim=digest, next_allowed=now + MIN_INTERVAL, last_attempt=receipt)
        _write(session, STATE, state)
        _write(session, RECEIPT + digest, receipt)
        session.commit()
    api, triggered = None, False
    try:
        api = _factory()
        triggered = True
        report = api.probe(acknowledge_customer_ids=True)
        if report.get("status") == "no_external_contact_relationship":
            raise WeComError("wecom_test_customer_relationship_required", 84061)
        if report.get("status") == "test_member_not_customer_contact_enabled":
            raise WeComError("wecom_test_member_not_enabled")
        counts = _counts(report)
        receipt.update(status="completed", counts=counts)
    except WeComError as error:
        code = error.code if isinstance(error.code, str) and re.fullmatch(r"wecom_[a-z0-9_]{1,80}", error.code) else "wecom_probe_failed"
        receipt.update(status="failed", error_code=code,
                       api_code=error.api_code if type(error.api_code) is int else None)
    except Exception:
        receipt.update(status="failed", error_code="wecom_probe_failed")
    finally:
        if api:
            try:
                api.close()
            except Exception:
                pass
    receipt["finished"] = _clock()
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        state = _state(session)
        if state["claim"] == digest:
            state.update(claim=None, last_attempt=receipt)
            if receipt["status"] == "completed":
                state["last_success"] = receipt
            _write(session, STATE, state)
        else:
            receipt = {"status": "failed", "started": now, "finished": _clock(),
                       "error_code": "wecom_claim_changed"}
        _write(session, RECEIPT + digest, receipt)
        session.commit()
        return _receipt_view(state, receipt, _clock(), replayed=False, triggered=triggered)


def _owner(request):
    # These endpoints do not inherit scoped employee/robot tokens.
    from .business_workflows import principal
    from .hub_adapter import local_request
    if (not local_request(request) or request.headers.get("authorization")
            or request.headers.get("x-creatorhub-team-gateway")):
        raise HTTPException(403, "wecom_local_owner_required")
    principal(request, {"admin"})


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_customer_ids: StrictBool

    @field_validator("acknowledge_customer_ids")
    @classmethod
    def approved(cls, value):
        if value is not True:
            raise ValueError("explicit acknowledgement required")
        return value


@router.get("/status")
def get_status(request: Request):
    _owner(request)
    return snapshot()


@router.get("/requests/{key}")
def get_request(key: str, request: Request):
    _owner(request)
    return request_status(key)


@router.post("/refresh")
def post_refresh(body: RefreshIn, request: Request):
    _owner(request)
    return refresh(request.headers.get("idempotency-key", ""))
