"""聊天导出：关键词解析、Excel / HTML 写出。"""
import pytest

from chat_export import (
    _parse_keywords, _match_keywords, _write_html_conversation,
    _write_xlsx, _write_csv, _write_txt,
)


class TestKeywordParse:
    def test_empty(self):
        assert _parse_keywords('') == ('and', [])
        assert _parse_keywords(None) == ('and', [])

    def test_and_by_space(self):
        mode, terms = _parse_keywords('会议 报销')
        assert mode == 'and'
        assert terms == ['会议', '报销']

    def test_or_by_pipe(self):
        mode, terms = _parse_keywords('合同|发票')
        assert mode == 'or'
        assert terms == ['合同', '发票']

    def test_match_and(self):
        assert _match_keywords('今天开会谈报销', 'and', ['会议', '报销']) is False
        assert _match_keywords('会议纪要含报销', 'and', ['会议', '报销']) is True

    def test_match_or(self):
        assert _match_keywords('合同已签', 'or', ['合同', '发票']) is True
        assert _match_keywords('你好', 'or', ['合同', '发票']) is False


def _sample_msgs():
    return [
        {
            'create_time': 1700000000,
            'timestamp': '2023-11-15 06:13:20',
            'date': '2023-11-15',
            'sender': '我',
            'is_me': True,
            'is_system': False,
            'type': 1,
            'type_name': '文本',
            'text': '你好，明天会议',
            'chat_name': '测试群',
            'chat_id': '123@chatroom',
            'is_group': True,
            'local_id': 1,
        },
        {
            'create_time': 1700000100,
            'timestamp': '2023-11-15 06:15:00',
            'date': '2023-11-15',
            'sender': '张三',
            'is_me': False,
            'is_system': False,
            'type': 1,
            'type_name': '文本',
            'text': '收到',
            'chat_name': '测试群',
            'chat_id': '123@chatroom',
            'is_group': True,
            'local_id': 2,
        },
    ]


@pytest.fixture
def meta():
    return {
        'display': '测试群',
        'uname': '123@chatroom',
        'start_ts': None,
        'end_ts': None,
        'keyword': '会议',
        'exported_at': '2026-01-01 12:00:00',
        'chat_count': 1,
    }


class TestWriters:
    def test_html_has_bubbles_and_search(self, tmp_path, meta):
        out = tmp_path / 'a.html'
        _write_html_conversation(str(out), _sample_msgs(), meta)
        html = out.read_text(encoding='utf-8')
        assert 'bubble' in html
        assert '明天会议' in html
        assert 'id="q"' in html
        assert 'mark' in html

    def test_xlsx_writes_rows(self, tmp_path, meta):
        openpyxl = pytest.importorskip('openpyxl')
        out = tmp_path / 'a.xlsx'
        _write_xlsx(str(out), _sample_msgs(), meta)
        wb = openpyxl.load_workbook(out)
        assert '聊天记录' in wb.sheetnames
        ws = wb['聊天记录']
        assert ws['E2'].value == '你好，明天会议'

    def test_csv_and_txt(self, tmp_path, meta):
        csv_path = tmp_path / 'a.csv'
        txt_path = tmp_path / 'a.txt'
        _write_csv(str(csv_path), _sample_msgs(), meta)
        _write_txt(str(txt_path), _sample_msgs(), meta)
        assert '明天会议' in csv_path.read_text(encoding='utf-8-sig')
        assert '张三' in txt_path.read_text(encoding='utf-8')
