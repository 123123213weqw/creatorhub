"""Content-bound review for CLI drafts, using the Hub's existing task table."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from fastapi import HTTPException

MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
EXTENSIONS = {"images": {".jpg", ".jpeg", ".png", ".webp"},
              "video": {".mp4", ".mov", ".m4v"}}


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def snapshot_media(paths, media_type, upload_dir):
    """Never publish the caller's mutable originals. Failed copies are removed."""
    if media_type not in EXTENSIONS or not 1 <= len(paths) <= 9:
        raise HTTPException(422, "素材类型或数量不正确（1–9 张图或 1 个视频）")
    if media_type == "video" and len(paths) != 1:
        raise HTTPException(422, "视频草稿只能包含一个文件")
    folder = Path(upload_dir).resolve() / "review-drafts" / uuid.uuid4().hex
    folder.mkdir(parents=True, mode=0o700)
    manifest, total = [], 0
    try:
        for i, raw in enumerate(paths):
            src = Path(raw).expanduser()
            if (not src.is_absolute() or src.is_symlink() or not src.is_file()
                    or src.suffix.lower() not in EXTENSIONS[media_type]):
                raise HTTPException(422, "素材必须是本机绝对路径的普通图片/视频文件，不接受链接")
            target = folder / f"{i}{src.suffix.lower()}"
            size = 0
            with open(src, "rb") as source, open(target, "xb") as dest:
                os.chmod(target, 0o600)
                while chunk := source.read(1024 * 1024):
                    size += len(chunk); total += len(chunk)
                    if size > MAX_FILE or total > MAX_TOTAL:
                        raise HTTPException(422, "素材超过单个 512 MiB / 总计 1 GiB 上限")
                    dest.write(chunk)
            if not size:
                raise HTTPException(422, "素材文件为空")
            manifest.append({"path": str(target), "name": src.name,
                             "size": size, "sha256": file_hash(target)})
        return manifest
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def content_digest(task):
    values = {key: getattr(task, key) for key in (
        "platform", "account_id", "media_type", "title", "desc", "topics",
        "location", "visibility", "allow_save", "scheduled_at_is_utc")}
    if task.demo_only:
        values["demo_only"] = True
    values["scheduled_at"] = task.scheduled_at.isoformat() if task.scheduled_at else None
    values["media"] = [{k: m[k] for k in ("size", "sha256")}
                       for m in json.loads(task.media_manifest)]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def media_intact(task):
    try:
        manifest = json.loads(task.media_manifest)
        paths = json.loads(task.media_json)
        return bool(manifest) and paths == [m["path"] for m in manifest] and all(
            not Path(m["path"]).is_symlink()
            and Path(m["path"]).stat().st_size == m["size"]
            and file_hash(m["path"]) == m["sha256"] for m in manifest)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def review_valid(task):
    try:
        if task.demo_only:
            return False
        return (not task.review_required or bool(
            task.approved_at and task.approved_digest == task.review_digest
            and task.review_digest == content_digest(task) and media_intact(task)))
    except (ValueError, TypeError, KeyError):
        return False


# Sparse business defaults, not platform safe thresholds. Legacy tasks unchanged.
PUBLISH_GLOBAL_GAP = timedelta(minutes=10)
PUBLISH_ACCOUNT_GAP = timedelta(minutes=30)
REVIEW_LATE_WINDOW = timedelta(minutes=30)


def review_expired(task, now):
    expected = task.scheduled_at or task.approved_at
    return bool(task.review_required and expected and now > expected + REVIEW_LATE_WINDOW)


def publish_slot(session, task, now, *, reserve=False):
    from .hub_adapter import PublishPacing
    gates = [session.get(PublishPacing, aid) for aid in (0, task.account_id)]
    due = max([now] + [g.next_allowed_at for g in gates if g])
    if due > now:
        return due
    if reserve:
        for aid, gap, gate in zip((0, task.account_id),
                                 (PUBLISH_GLOBAL_GAP, PUBLISH_ACCOUNT_GAP), gates):
            gate = gate or PublishPacing(account_id=aid, next_allowed_at=now)
            gate.next_allowed_at = now + gap
            session.add(gate)
    return None
