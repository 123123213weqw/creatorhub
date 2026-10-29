import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import db
from app.config import Config
from app.engine.collection import (
    KeywordCollector,
    _content_error_after_download,
    _tiktok_download_session,
)
from app.engine.downloader import Downloader
from app.models import DouyinAccount, KeywordCollectionContent, KeywordCollectionJob
from app.platforms.douyin import Aweme, MediaItem


def _tiktok_aweme() -> Aweme:
    aweme = Aweme(
        aweme_id="731", desc="fixture", create_time=0,
        author_name="alice", media_type="video",
    )
    aweme.platform = "tiktok"
    aweme.source_url = "https://www.tiktok.com/@alice/video/731"
    aweme.medias = [MediaItem(
        url="https://v16-webapp-prime.tiktok.com/video/fixture",
        kind="video", ext="mp4", index=0,
    )]
    return aweme


def test_downloader_sends_tiktok_referer_cookie_and_account_ua(tmp_path):
    downloader = Downloader(str(tmp_path), "global-agent")

    async def capture(client, _url, path):
        assert client.headers["user-agent"] == "account-agent"
        assert client.headers["referer"] == (
            "https://www.tiktok.com/@alice/video/731")
        assert client.cookies.get("sessionid") == "authenticated"
        path.write_bytes(b"video")
        return ""

    downloader._download_one = capture
    ok, path, error = asyncio.run(downloader.download_aweme(
        _tiktok_aweme(), user_agent="account-agent",
        cookies={"sessionid": "authenticated"},
        referer="https://www.tiktok.com/@alice/video/731",
    ))

    assert ok and not error
    assert Path(path).read_bytes() == b"video"


def test_tiktok_download_session_merges_domain_scoped_account_states():
    account = SimpleNamespace(
        ua="stored-agent",
        storage_state=json.dumps({"cookies": [
            {"name": "sessionid", "value": "main",
             "domain": ".tiktok.com"},
            {"name": "foreign", "value": "ignored",
             "domain": "accounts.google.com"},
        ]}),
        creator_storage_state=json.dumps({"cookies": [
            {"name": "msToken", "value": "creator",
             "domain": "www.tiktok.com"},
        ]}),
    )

    user_agent, cookies = _tiktok_download_session(
        account, SimpleNamespace(ua="identity-agent"), "global-agent")

    assert user_agent == "identity-agent"
    assert cookies == {"sessionid": "main", "msToken": "creator"}


def test_successful_retry_removes_only_stale_download_error():
    assert _content_error_after_download(
        "详情字段缺失; 下载失败(HTTP 403): https://cdn.test/video", "") == (
            "详情字段缺失")
    assert _content_error_after_download(
        "详情字段缺失; 下载失败(HTTP 403): https://cdn.test/video",
        "下载失败(HTTP 502): https://cdn.test/video",
    ) == "详情字段缺失; 下载失败(HTTP 502): https://cdn.test/video"


def test_keyword_download_injects_tiktok_account_session(tmp_path):
    previous_engine = db._engine
    db.init_db(str(tmp_path / "collection.db"))
    try:
        with db.get_session() as session:
            account_row = DouyinAccount(
                platform="tiktok", nickname="fixture", status="active",
                storage_state="{}",
            )
            session.add(account_row)
            session.commit()
            session.refresh(account_row)
            job = KeywordCollectionJob(
                platform="tiktok", account_id=account_row.id,
                keywords='["fixture"]',
                download_media=True, download_dir=str(tmp_path),
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            content = KeywordCollectionContent(
                job_id=job.id, platform="tiktok", keyword="fixture",
                aweme_id="731", download_status="pending",
            )
            session.add(content)
            session.commit()
            session.refresh(content)
            job_id = job.id
            content_id = content.id

        downloader = SimpleNamespace(download_aweme=AsyncMock(
            return_value=(True, str(tmp_path / "731.mp4"), "")))
        collector = KeywordCollector(Config(), SimpleNamespace(), downloader)
        account = SimpleNamespace(
            ua="stored-agent",
            storage_state=json.dumps({"cookies": [
                {"name": "sessionid", "value": "authenticated",
                 "domain": ".tiktok.com"},
            ]}),
            creator_storage_state="",
        )
        job = SimpleNamespace(
            id=job_id, platform="tiktok", download_media=True,
            download_dir=str(tmp_path),
        )
        content = SimpleNamespace(
            id=content_id, keyword="fixture", aweme_id="731",
            download_status="pending",
        )

        asyncio.run(collector._download(
            job, content, _tiktok_aweme(), "http://proxy.test:8080",
            account=account, identity=SimpleNamespace(ua="identity-agent"),
        ))

        downloader.download_aweme.assert_awaited_once()
        args = downloader.download_aweme.await_args
        assert args.kwargs["user_agent"] == "identity-agent"
        assert args.kwargs["cookies"] == {"sessionid": "authenticated"}
        assert args.kwargs["referer"] == (
            "https://www.tiktok.com/@alice/video/731")
        assert args.args[2] == "http://proxy.test:8080"
        with db.get_session() as session:
            saved = session.get(KeywordCollectionContent, content_id)
            assert saved.download_status == "done"
    finally:
        if db._engine is not None:
            db._engine.dispose()
        db._engine = previous_engine
