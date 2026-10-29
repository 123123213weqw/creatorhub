from pathlib import Path


def test_tiktok_action_selectors_and_uncertain_contract_exist():
    source = Path("app/browser/tiktok_actions.py").read_text(encoding="utf-8")
    selectors = Path("app/platforms/tiktok/selectors.py").read_text(encoding="utf-8")
    assert 'button[data-e2e="message-button"]' in selectors
    assert 'iframe[data-testid*="message-index"]' in selectors
    assert 'data-e2e="message-input-area"' in selectors
    assert "write_uncertain:评论已提交但未取得评论 ID" in source
    assert "write_uncertain:私信已提交" in source
    assert 'target_url: str = ""' in source
    assert 'or video_url(aweme_id, author_unique_id)' in source
