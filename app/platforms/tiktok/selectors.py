"""TikTok Web selectors kept in one change-tolerant module."""

FOLLOW_BUTTONS = (
    'button[data-e2e="follow-button"]',
    'button[data-e2e="follow-btn"]',
    'button:has-text("Follow")',
    'button:has-text("Following")',
)
MESSAGE_BUTTONS = (
    'button[data-e2e="message-button"]',
    'button:has-text("Message")',
)
MESSAGE_IFRAME = 'iframe[data-testid*="message-index"]'
MESSAGE_INPUTS = (
    '[data-e2e="message-input-area"][contenteditable="true"]',
    'div[contenteditable="true"][data-e2e*="message"]',
    'textarea[placeholder*="message" i]',
    'div[contenteditable="true"]',
)
COMMENT_INPUTS = (
    '[data-e2e="comment-input"] [contenteditable="true"]',
    '[data-e2e="comment-input"] textarea',
    'div[contenteditable="true"][data-e2e*="comment"]',
    'textarea[placeholder*="comment" i]',
)
COMMENT_SUBMITS = (
    'button[data-e2e="comment-post"]',
    'button:has-text("Post")',
)
