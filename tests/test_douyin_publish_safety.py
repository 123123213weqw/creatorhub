"""No real browser, account, uploads or publication. Verify submit boundaries."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.platforms.douyin import publish as p


class Many:
    def __init__(self, items): self.items = items
    async def count(self): return len(self.items)
    @property
    def first(self): return self.items[0]
    def nth(self, index): return self.items[index]


class Radio:
    def __init__(self, text, *, checked=False, changes=True, native=True, aria=None, children=()):
        self.text = text; self.checked = checked; self.changes = changes
        self.native = native; self.aria = aria; self.children = children; self.clicks = 0
    async def is_checked(self, **kwargs):
        if not self.native: raise ValueError('custom wrapper')
        return self.checked
    async def get_attribute(self, key, **kwargs): return self.aria
    def locator(self, selector): return Many(list(self.children))
    async def scroll_into_view_if_needed(self, **kwargs): pass
    async def check(self, **kwargs):
        if not self.native: raise ValueError('custom wrapper')
        self.clicks += 1
        if self.changes: self.checked = True
    async def click(self, **kwargs):
        self.clicks += 1
        if self.changes: self.checked = True
    async def inner_text(self, **kwargs): return self.text


class Radios:
    def __init__(self, role=(), semi=()): self.role = list(role); self.semi = list(semi)
    def get_by_role(self, role, name, exact): return Many([x for x in self.role if x.text == name])
    def locator(self, selector): return Many(self.semi)
    async def eval_on_selector_all(self, *args): return []


@pytest.mark.parametrize('visibility,label', [('private', '仅自己可见'), ('friends', '好友可见'), ('public', '公开')])
@pytest.mark.parametrize('allow_save', [True, False])
def test_visibility_and_save_permission_must_be_selected(visibility, label, allow_save):
    page = Radios([Radio(label), Radio('允许' if allow_save else '不允许')])
    assert asyncio.run(p._apply_publish_settings(page, visibility, allow_save)) is True
    assert all(x.checked for x in page.role)


@pytest.mark.parametrize('visibility,allow_save', [('invalid', False), ('private', 0), ('private', None)])
def test_invalid_settings_never_click(visibility, allow_save):
    page = Radios([Radio('仅自己可见'), Radio('不允许')])
    assert asyncio.run(p._apply_publish_settings(page, visibility, allow_save)) is False
    assert all(x.clicks == 0 for x in page.role)


def test_click_success_without_selected_state_fails():
    radio = Radio('仅自己可见', changes=False)
    assert asyncio.run(p._choose_radio(Radios([radio]), radio.text)) is False
    assert radio.clicks == 1


def test_ambiguous_exact_role_does_not_fall_back_to_first():
    items = [Radio('仅自己可见'), Radio('仅自己可见')]
    page = Radios(items, [Radio('仅自己可见', checked=True)])
    assert asyncio.run(p._choose_radio(page, '仅自己可见')) is False
    assert all(x.clicks == 0 for x in items)


def test_custom_wrapper_requires_checked_child_or_aria():
    child = Radio('native', checked=True)
    wrapper = Radio('仅自己可见', native=False, children=[child])
    assert asyncio.run(p._choose_radio(Radios(semi=[wrapper]), wrapper.text)) is True
    assert asyncio.run(p._radio_selected(Radio('x', native=False, aria='true'))) is True
    assert asyncio.run(p._radio_selected(Radio('x', native=False, aria='false'))) is False


def test_duplicate_semantic_text_is_ambiguous():
    items = [Radio('不允许'), Radio('不允许')]
    assert asyncio.run(p._choose_radio(Radios(semi=items), '不允许')) is False
    assert all(x.clicks == 0 for x in items)


def test_missing_or_excessive_options_is_unknown_not_public():
    assert asyncio.run(p._apply_publish_settings(Radios(), 'private', False)) is False
    assert asyncio.run(p._radio_for_label(Radios(semi=[Radio('x')] * 65), 'x')) is None


@pytest.fixture
def browser_fixture(tmp_path, monkeypatch):
    media = tmp_path / 'video.mp4'; media.write_bytes(b'OFFLINE FIXTURE')
    page = SimpleNamespace(url=p.UPLOAD_URL, new_page=None)
    page.goto = AsyncMock(); page.wait_for_timeout = AsyncMock()
    page.on = Mock()
    page.locator = Mock(return_value=SimpleNamespace(first=SimpleNamespace(set_input_files=AsyncMock())))
    ctx = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    manager = SimpleNamespace(open_headed=AsyncMock(return_value=ctx))
    button = SimpleNamespace(is_enabled=AsyncMock(return_value=True),
        scroll_into_view_if_needed=AsyncMock(), click=AsyncMock())
    monkeypatch.setattr(p, '_wait_editor', AsyncMock(return_value=True))
    monkeypatch.setattr(p, '_fill_first', AsyncMock(return_value=True))
    monkeypatch.setattr(p, '_apply_publish_settings', AsyncMock(return_value=True))
    monkeypatch.setattr(p, '_publish_settings_confirmed', AsyncMock(return_value=True))
    monkeypatch.setattr(p, '_primary_publish_button', AsyncMock(return_value=button))
    monkeypatch.setattr(p, '_visible_any', AsyncMock(return_value='发布成功'))
    monkeypatch.setattr(p, '_dump', AsyncMock(return_value=''))
    monkeypatch.setattr(p, '_click_first', AsyncMock(return_value=True))
    async def run(**kwargs):
        return await p.publish_douyin(manager, object(), 'fixture', 'video', 'title', 'body',
            [str(media)], visibility='private', allow_save=False, **kwargs)
    return manager, ctx, page, button, run


def test_unconfirmed_setting_never_reaches_submit(browser_fixture, monkeypatch):
    manager, ctx, page, button, run = browser_fixture
    monkeypatch.setattr(p, '_apply_publish_settings', AsyncMock(return_value=False))
    result = asyncio.run(run())
    assert not result[0] and result[2].startswith('publish_settings_unconfirmed:')
    button.click.assert_not_awaited(); p._primary_publish_button.assert_not_awaited()
    ctx.close.assert_awaited_once()


def test_transcode_rerender_resets_private_and_blocks_submit(browser_fixture, monkeypatch):
    manager, ctx, page, button, run = browser_fixture
    monkeypatch.setattr(p, '_publish_settings_confirmed', AsyncMock(return_value=False))
    callback = Mock()
    result = asyncio.run(run(on_submit=callback))
    assert not result[0] and '未提交' in result[2]
    callback.assert_not_called(); button.click.assert_not_awaited()


def test_durable_marker_precedes_single_click(browser_fixture):
    manager, ctx, page, button, run = browser_fixture
    order = []
    button.click.side_effect = lambda **kwargs: order.append('click')
    result = asyncio.run(run(on_submit=lambda: order.append('durable_marker')))
    assert result[0] and order == ['durable_marker', 'click']
    button.click.assert_awaited_once()


def test_click_timeout_never_clicks_again_and_is_uncertain(browser_fixture):
    manager, ctx, page, button, run = browser_fixture
    button.click.side_effect = TimeoutError('acknowledgement lost')
    result = asyncio.run(run())
    assert not result[0] and result[2].startswith('write_uncertain:')
    button.click.assert_awaited_once(); p._click_first.assert_not_awaited()


def test_post_click_exception_is_uncertain(browser_fixture, monkeypatch):
    monkeypatch.setattr(p, '_visible_any', AsyncMock(side_effect=RuntimeError('lost page')))
    result = asyncio.run(browser_fixture[-1]())
    assert not result[0] and result[2].startswith('write_uncertain:')


def test_marker_failure_never_submits(browser_fixture):
    callback = Mock(side_effect=RuntimeError('DB unavailable'))
    result = asyncio.run(browser_fixture[-1](on_submit=callback))
    assert not result[0]
    browser_fixture[3].click.assert_not_awaited()


@pytest.mark.parametrize('failed_fill', [1, 2])
def test_content_fill_failure_never_submits(browser_fixture, monkeypatch, failed_fill):
    monkeypatch.setattr(p, '_fill_first', AsyncMock(side_effect=[False] if failed_fill == 1 else [True, False]))
    result = asyncio.run(browser_fixture[-1]())
    assert not result[0] and result[2].startswith('publish_content_unconfirmed:')
    browser_fixture[3].click.assert_not_awaited()


def test_cancel_propagates_and_releases_context(browser_fixture):
    browser_fixture[3].click.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(browser_fixture[-1]())
    browser_fixture[1].close.assert_awaited_once()
