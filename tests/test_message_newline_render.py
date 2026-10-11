"""回归：聊天气泡里的换行必须像电脑版微信那样真实换行。

历史缺陷：`.msg-text` 没有声明 `white-space`，CSS 默认值 `normal` 会把正文里的
换行折叠成空格，于是多行消息在 Web UI 里被压成一行。
（`.msg-bubble-detail` 的正文路径早已用 `white-space:pre-wrap`，只有气泡这条漏了。）

这里锁定 CSS 契约，避免将来被"清理样式"时又退回折叠。
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(REPO, 'src', 'web', 'static', 'css', 'app.css')
BUBBLE_JS = os.path.join(REPO, 'src', 'web', 'static', 'js', 'components', 'message-bubble.js')


def _rule(css_text, selector):
    """取出某个选择器的声明块。

    用「前置字符不是标识符字符、后置也不能继续拼标识符」来定位，避免匹配到
    `.msg-text-foo` 这类子串；同时不依赖规则前面是 `}` —— 规则前面可能是注释块。
    """
    pattern = r'(?<![\w.\-#])' + re.escape(selector) + r'(?![\w-])\s*\{([^}]*)\}'
    m = re.search(pattern, css_text)
    return m.group(1) if m else None


def test_msg_text_preserves_newlines():
    css = open(CSS, encoding='utf-8').read()
    body = _rule(css, '.msg-text')
    assert body is not None, '.msg-text 规则不存在'
    assert 'white-space' in body, (
        '.msg-text 必须声明 white-space: pre-wrap，否则正文换行会被 CSS 折叠成空格')
    value = re.search(r'white-space\s*:\s*([a-z-]+)', body).group(1)
    assert value == 'pre-wrap', 'white-space 应为 pre-wrap，实际 %s' % value


def test_msg_text_still_wraps_long_lines():
    """开启 pre-wrap 后仍需保证长串（URL、无空格长文本）能折行，不能撑破气泡。"""
    css = open(CSS, encoding='utf-8').read()
    body = _rule(css, '.msg-text')
    assert 'word-break' in body or 'overflow-wrap' in body, (
        '.msg-text 需要 word-break/overflow-wrap，否则长串不折行会撑破气泡宽度')


def test_bubble_text_path_uses_display_content():
    """气泡正文必须走已剥离发送者前缀的 content，不得回退到带 wxid 前缀的原文。"""
    js = open(BUBBLE_JS, encoding='utf-8').read()
    assert 'm.content_raw' not in js.split('_renderBubble')[1][:2000], (
        '气泡正文不应使用 content_raw（它保留了 "sender_wxid:\\n" 前缀）')
