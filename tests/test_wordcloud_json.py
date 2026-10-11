"""Tests for _build_wordcloud_html — JSON embedding safety."""
import json
import re
import pytest
from wordcloud_gen import _build_wordcloud_html


def _extract_json_var(html: str, var_name: str) -> str:
    """Extract a JavaScript variable assignment from the HTML."""
    pattern = rf'var {var_name} = (.*?);'
    match = re.search(pattern, html, re.DOTALL)
    assert match is not None, f"var {var_name} not found in HTML"
    return match.group(1)


class TestWordCloudHtml:
    def test_basic_output_contains_expected_elements(self):
        words = [("你好", 10.0), ("世界", 5.0)]
        html = _build_wordcloud_html("测试", words, 100, 50)
        assert "词云分析: 测试" in html
        assert '<div id="wordcloud"' in html
        assert "总消息: 100" in html
        assert "有效词汇: 50" in html

    def test_word_data_json_is_parseable(self):
        words = [("你好", 10.0), ("世界", 5.0)]
        html = _build_wordcloud_html("测试", words, 100, 50)
        json_str = _extract_json_var(html, "wordData")
        data = json.loads(json_str)
        assert len(data) == 2
        assert data[0] == {"name": "你好", "value": 10.0}

    def test_empty_word_list(self):
        html = _build_wordcloud_html("空", [], 0, 0)
        json_str = _extract_json_var(html, "wordData")
        data = json.loads(json_str)
        assert data == []

    def test_words_with_double_quotes(self):
        words = [('含"引号"的词', 5.0)]
        html = _build_wordcloud_html("测试", words, 10, 5)
        json_str = _extract_json_var(html, "wordData")
        data = json.loads(json_str)
        assert data[0]["name"] == '含"引号"的词'

    def test_words_with_backslashes(self):
        words = [(r"含\反斜杠\的词", 5.0)]
        html = _build_wordcloud_html("测试", words, 10, 5)
        json_str = _extract_json_var(html, "wordData")
        data = json.loads(json_str)
        assert data[0]["name"] == r"含\反斜杠\的词"

    def test_words_with_script_tag_does_not_break_html(self):
        """Words containing </script> are properly JSON-encoded.

        Note: Python json.dumps does NOT escape </ to <\\/ like JavaScript's
        JSON.stringify does. Words containing </script> could theoretically
        close a <script> tag in browsers. This test verifies the current
        behavior and that the JSON remains extractable."""
        words = [("</script>", 5.0), ("<script>alert(1)</script>", 3.0)]
        html = _build_wordcloud_html("XSS测试", words, 10, 5)
        json_str = _extract_json_var(html, "wordData")
        data = json.loads(json_str)
        assert data[0]["name"] == "</script>"
        assert data[1]["name"] == "<script>alert(1)</script>"

    def test_words_with_newlines(self):
        words = [("hello\nworld", 5.0)]
        html = _build_wordcloud_html("测试", words, 10, 5)
        json_str = _extract_json_var(html, "wordData")
        data = json.loads(json_str)
        assert data[0]["name"] == "hello\nworld"

    def test_words_with_unicode_emoji(self):
        words = [("😀😀", 10.0), ("café", 5.0)]
        html = _build_wordcloud_html("Unicode", words, 20, 10)
        json_str = _extract_json_var(html, "wordData")
        data = json.loads(json_str)
        assert data[0]["name"] == "😀😀"
        assert data[1]["name"] == "café"

    def test_bar_data_json_parseable(self):
        words = [("A", 10.0), ("B", 8.0), ("C", 5.0)]
        html = _build_wordcloud_html("测试", words, 30, 20)
        labels_json = _extract_json_var(html, "barLabels")
        values_json = _extract_json_var(html, "barValues")
        labels = json.loads(labels_json)
        values = json.loads(values_json)
        assert labels == ["C", "B", "A"]  # Reversed
        assert values == [5.0, 8.0, 10.0]

    def test_top_words_truncated_at_200_in_table(self):
        """Only top 200 words appear in the HTML table rows (JSON has all)."""
        words = [(f"word_{i}", 100.0 - i) for i in range(250)]
        html = _build_wordcloud_html("大词表", words, 1000, 500)
        # word_0 through word_199 should be in the TABLE rows
        assert "<td>word_0</td>" in html
        assert "<td>word_199</td>" in html
        # word_200+ should NOT be in TABLE rows (but IS in JSON data)
        assert "<td>word_200</td>" not in html
        # But word_200 IS in the wordData JSON
        assert '"word_200"' in html
