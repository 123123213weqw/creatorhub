"""Freeze existing Hub assets; validate WeCom's small-image/video contract."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess

from fastapi import HTTPException
from PIL import Image

from .models import LocalMediaAsset
from .publish_review import snapshot_media

MAX_BYTES = 10_000_000  # Conservative decimal 10 MB, never silently transcode.


def media_bytes(item, root):
    try:
        path = Path(item['path'])
        if (not path.is_absolute() or path.is_symlink()
                or not path.resolve().is_relative_to(Path(root).resolve())
                or type(item['size']) is not int or not 5 < item['size'] <= MAX_BYTES):
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != item['size']:
                raise ValueError()
            value = stream.read(MAX_BYTES + 1)
        if len(value) != item['size'] or hashlib.sha256(value).hexdigest() != item['sha256']:
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise HTTPException(409, '企微朋友圈素材已变化或不可用，请重新登记并审核') from None


def inspect_media(item, kind, root):
    content = media_bytes(item, root)
    if kind == 'images':
        try:
            with Image.open(io.BytesIO(content)) as image:
                fmt, (width, height) = image.format, image.size
                image.verify()
            if (fmt not in {'PNG', 'JPEG'} or min(width, height) > 1080
                    or max(width, height) > 10800 or min(width, height) <= 0):
                raise ValueError()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(422, '朋友圈图片须为JPG/PNG，短边≤1080、长边≤10800，不自动修改图片') from None
        return {**item, 'media_type': 'images', 'format': fmt, 'width': width, 'height': height}
    if kind != 'video' or Path(item['path']).suffix.lower() != '.mp4':
        raise HTTPException(422, '朋友圈视频只接受MP4，不自动转码')
    executable = shutil.which('ffprobe')
    if not executable:
        raise HTTPException(422, '缺少ffprobe，无法核验视频时长；不会绕过校验或自动安装')
    try:
        result = subprocess.run([executable, '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-show_entries', 'format=format_name,duration:stream=codec_type', '-of', 'json',
            item['path']], capture_output=True, timeout=8, check=True)
        if len(result.stdout) > 65536:
            raise ValueError()
        data = json.loads(result.stdout)
        seconds = float(data['format']['duration'])
        if (not math.isfinite(seconds) or not 0 < seconds <= 30
                or 'mp4' not in data['format']['format_name'].split(',')
                or not any(s.get('codec_type') == 'video' for s in data['streams'])):
            raise ValueError()
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        raise HTTPException(422, '朋友圈视频需为含视频轨的有效MP4，时长不超过30秒') from None
    return {**item, 'media_type': 'video', 'format': 'MP4', 'duration_ms': math.ceil(seconds * 1000)}


def freeze_assets(session, asset_ids, kind, root):
    if kind == 'text':
        if asset_ids:
            raise HTTPException(422, '纯文字草稿不能携带素材')
        return [], False
    if not asset_ids or len(set(asset_ids)) != len(asset_ids):
        raise HTTPException(422, '请指定不重复的已登记素材编号')
    paths, demo = [], False
    for aid in asset_ids:
        asset = session.get(LocalMediaAsset, aid)
        if not asset or asset.media_type != kind:
            raise HTTPException(422, '素材编号不存在或类型不匹配')
        demo = demo or asset.is_demo
        for original in json.loads(asset.manifest_json):
            # Verify the registered copy before creating a new immutable snapshot.
            media_bytes(original, root)
            paths.append(original['path'])
    if not 1 <= len(paths) <= (1 if kind == 'video' else 9):
        raise HTTPException(422, '朋友圈最多9张图片或1个视频，不混用类型')
    copied = snapshot_media(paths, kind, root)
    try:
        checked = [inspect_media(item, kind, root) for item in copied]
        return checked, demo
    except BaseException:
        # Only the UUID directory just created by snapshot_media is disposable.
        shutil.rmtree(Path(copied[0]['path']).parent)
        raise


def media_digest_items(items):
    return [{k: v for k, v in item.items() if k not in {'path', 'name'}} for item in items]
