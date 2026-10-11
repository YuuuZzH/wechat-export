"""通讯录导出（xlsx / csv / html）回归测试。

背景：通讯录此前只有 Web 端一个硬编码 CSV 导出（`/api/address-book/export`），
且输出没有 UTF-8 BOM —— Excel 双击打开中文全是乱码；命令行则完全没有通讯录导出。
本模块为 CLI 与 Web 提供同一套渲染逻辑，故在此统一回归。

全部使用合成数据，不触碰真实微信数据（隐私红线）。
"""
import csv
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import contacts_export as ce
from engine.services.address_book import filter_contacts
from engine.constants import TZ


# --------------------------------------------------------------------------
# 合成数据：字段与 engine.services.address_book.get_all_contacts() 的输出形状一致
# --------------------------------------------------------------------------

def _contact(**kw):
    base = {
        'wxid': 'wxid_x',
        'display_name': '某人',
        'remark': '',
        'nick_name': '',
        'alias': '',
        'avatar_url': '/api/avatar/wxid_x',
        'msg_count': 0,
        'last_msg_time': None,
        'is_group': False,
        'labels': [],
    }
    base.update(kw)
    return base


@pytest.fixture
def sample():
    """3 个联系人 + 2 个群聊，含中文、特殊字符、空字段。"""
    return [
        _contact(wxid='wxid_alpha', display_name='李老师', remark='李老师', nick_name='李',
                 alias='lisi', phone='+86 13800001111', sex=1,
                 country='中国', province='广东', city='深圳',
                 signature='静水流深', description='同事', msg_count=1200,
                 last_msg_time=1789531971),
        _contact(wxid='wxid_beta', display_name='张三', remark='', nick_name='张三',
                 alias='zhangsan', sex=2, province='北京', city='北京',
                 msg_count=7, last_msg_time=1700000000),
        _contact(wxid='wxid_gamma', display_name='<script>alert(1)</script>',
                 remark='XSS & "引号"', nick_name='', alias='',
                 msg_count=0, last_msg_time=None),
        _contact(wxid='12345678@chatroom', display_name='相亲相爱一家人',
                 remark='', nick_name='', alias='', msg_count=999, is_group=True,
                 last_msg_time=1789600000),
        _contact(wxid='87654321@chatroom', display_name='项目群',
                 remark='', nick_name='', alias='', msg_count=0, is_group=True),
    ]


# --------------------------------------------------------------------------
# 列定义 / 取值
# --------------------------------------------------------------------------

class TestColumns:
    # 旧版硬编码 CSV 的 13 列，顺序不可变
    LEGACY = [
        'wxid', 'display_name', 'remark', 'nick_name', 'alias',
        'phone', 'sex', 'region', 'signature', 'description',
        'msg_count', 'last_msg_time', 'is_group',
    ]

    def test_headers_keep_legacy_order(self):
        """前若干列必须与旧的硬编码 CSV 完全一致，否则下游表格错位。"""
        assert ce.COLUMNS[:len(self.LEGACY)] == self.LEGACY

    def test_new_columns_appended_at_end(self):
        """新列只能追加在末尾，不能插队。"""
        assert ce.COLUMNS[len(self.LEGACY):] == ['labels']

    def test_label_cell_joins_names(self, sample):
        got = ce.build_rows([_contact(labels=['non_work', 'only_work'])])
        assert got[0][ce.COLUMNS.index('labels')] == 'non_work,only_work'

    def test_label_cell_empty_when_no_labels(self):
        got = ce.build_rows([_contact()])
        assert got[0][ce.COLUMNS.index('labels')] == ''

    def test_sex_comes_from_parsed_extra_buffer(self):
        """sex 现在有真实来源（extra_buffer field 2），不再是永远为空。"""
        got = ce.build_rows([_contact(sex=2), _contact(sex=1), _contact(sex=0)])
        assert [r[ce.COLUMNS.index('sex')] for r in got] == ['女', '男', '']

    def test_region_from_country_province_city(self):
        got = ce.build_rows([_contact(country='CN', province='Shaanxi', city="Xi'an")])
        assert got[0][ce.COLUMNS.index('region')] == "CN Shaanxi Xi'an"

    def test_sex_label(self):
        assert ce.sex_label(1) == '男'
        assert ce.sex_label(2) == '女'
        assert ce.sex_label(0) == ''
        assert ce.sex_label(None) == ''
        assert ce.sex_label('1') == '男'      # 字符串也认

    def test_region_joins_present_parts_only(self, sample):
        assert ce.region_of(sample[0]) == '中国 广东 深圳'
        assert ce.region_of(sample[1]) == '北京 北京'   # 无 country
        assert ce.region_of(sample[2]) == ''             # 全空
        # region 字段本身也认（部分数据源直接给 region）
        assert ce.region_of({'region': '上海'}) == '上海'

    def test_build_rows_shape(self, sample):
        rows = ce.build_rows(sample)
        assert len(rows) == len(sample)
        assert all(len(r) == len(ce.COLUMNS) for r in rows)

    def test_build_rows_legacy_values(self, sample):
        """CSV 值必须保持旧端点语义：epoch 时间 + Y/N + 男女标签。"""
        rows = ce.build_rows(sample)
        assert rows[0][5] == '+86 13800001111'
        assert rows[0][6] == '男'
        assert rows[0][7] == '中国 广东 深圳'
        assert rows[0][11] == 1789531971        # 原始 epoch，不是格式化字符串
        assert rows[0][12] == 'N'
        assert rows[3][12] == 'Y'
        assert rows[2][11] == ''                # 无时间 → 空串


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

class TestCsv:
    def test_has_utf8_bom(self, sample):
        """带 BOM，Excel 双击才不乱码（旧实现缺 BOM，是本次顺带修复的缺陷）。"""
        data = ce.render_csv(sample)
        assert isinstance(data, bytes)
        assert data.startswith(b'\xef\xbb\xbf')

    def test_roundtrip(self, sample):
        data = ce.render_csv(sample)
        rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))
        assert rows[0] == ce.COLUMNS
        assert len(rows) == len(sample) + 1
        assert rows[1][1] == '李老师'           # 中文未被破坏
        assert rows[3][1] == '<script>alert(1)</script>'   # CSV 不做 HTML 转义

    def test_quotes_and_commas_survive(self):
        data = ce.render_csv([_contact(display_name='甲,乙', remark='说"你好"')])
        rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))
        assert rows[1][1] == '甲,乙'
        assert rows[1][2] == '说"你好"'

    def test_empty_list_has_header_only(self):
        data = ce.render_csv([])
        rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))
        assert rows == [ce.COLUMNS]


# --------------------------------------------------------------------------
# XLSX
# --------------------------------------------------------------------------

class TestXlsx:
    def _load(self, data):
        openpyxl = pytest.importorskip('openpyxl')
        return openpyxl.load_workbook(io.BytesIO(data))

    def test_roundtrip(self, sample):
        wb = self._load(ce.render_xlsx(sample))
        ws = wb.active
        assert ws.max_row == len(sample) + 1
        assert ws.max_column == len(ce.COLUMNS)
        assert [c.value for c in ws[1]] == ce.COLUMNS
        assert ws.cell(row=2, column=2).value == '李老师'

    def test_msg_count_is_numeric(self, sample):
        """xlsx 里消息数存数字，方便排序/求和（CSV 保持字符串以兼容旧端点）。"""
        ws = self._load(ce.render_xlsx(sample)).active
        cell = ws.cell(row=2, column=ce.COLUMNS.index('msg_count') + 1)
        assert cell.value == 1200
        assert isinstance(cell.value, int)

    def test_time_is_readable(self, sample):
        from datetime import datetime
        ws = self._load(ce.render_xlsx(sample)).active
        cell = ws.cell(row=2, column=ce.COLUMNS.index('last_msg_time') + 1)
        expect = datetime.fromtimestamp(1789531971, TZ).strftime('%Y-%m-%d %H:%M:%S')
        assert cell.value == expect
        # 无时间的行留空而不是 0/None 字面量
        empty = ws.cell(row=4, column=ce.COLUMNS.index('last_msg_time') + 1)
        assert empty.value in (None, '')

    def test_header_frozen(self, sample):
        ws = self._load(ce.render_xlsx(sample)).active
        assert ws.freeze_panes == 'A2'

    def test_empty_list_is_valid_workbook(self):
        wb = self._load(ce.render_xlsx([]))
        ws = wb.active
        assert ws.max_row == 1
        assert [c.value for c in ws[1]] == ce.COLUMNS

    def test_long_chinese_not_truncated(self):
        name = '王' * 40
        ws = self._load(ce.render_xlsx([_contact(display_name=name)])).active
        assert ws.cell(row=2, column=2).value == name


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

class TestHtml:
    def test_escapes_xss(self, sample):
        html = ce.render_html(sample)
        assert '<script>alert(1)</script>' not in html
        assert '&lt;script&gt;' in html

    def test_escapes_attribute_unsafe_chars(self):
        html = ce.render_html([_contact(display_name='a&b<c>d"e\'f')])
        assert 'a&amp;b&lt;c&gt;d' in html

    def test_contains_all_rows(self, sample):
        html = ce.render_html(sample)
        for c in sample:
            assert ce._escape_html(c['wxid']) in html
        assert '相亲相爱一家人' in html

    def test_self_contained(self, sample):
        """单文件、离线可用：不得引用任何外部资源。"""
        html = ce.render_html(sample)
        assert '<meta charset="utf-8">' in html.lower()
        assert 'http://' not in html
        assert 'https://' not in html
        assert '<link' not in html.lower()
        assert '<script' not in html.lower()

    def test_reports_total(self, sample):
        html = ce.render_html(sample)
        assert str(len(sample)) in html
        assert '通讯录' in html

    def test_empty_list_renders(self):
        html = ce.render_html([])
        assert ce.COLUMNS[1] in html
        assert '0' in html


# --------------------------------------------------------------------------
# 统一入口 / 文件名
# --------------------------------------------------------------------------

class TestRender:
    @pytest.mark.parametrize('fmt', ['csv', 'xlsx', 'html'])
    def test_returns_bytes_with_mimetype(self, sample, fmt):
        data = ce.render(sample, fmt)
        assert isinstance(data, bytes)
        assert fmt in ce.MIMETYPES

    def test_format_is_case_insensitive(self, sample):
        # 不比字节：openpyxl 会把 created/modified 写成当前时间
        openpyxl = pytest.importorskip('openpyxl')
        a = openpyxl.load_workbook(io.BytesIO(ce.render(sample, 'XLSX'))).active
        b = openpyxl.load_workbook(io.BytesIO(ce.render(sample, 'xlsx'))).active
        assert a.max_row == b.max_row
        assert [c.value for c in a[1]] == [c.value for c in b[1]]

    def test_unknown_format_raises(self, sample):
        with pytest.raises(ValueError):
            ce.render(sample, 'pdf')

    def test_xlsx_mimetype_is_excel(self):
        assert 'spreadsheetml' in ce.MIMETYPES['xlsx']

    def test_default_filename(self):
        name = ce.default_filename('xlsx', kind='all',
                                   now=__import__('datetime').datetime(2026, 9, 21, 14, 30, 0))
        assert name == 'contacts_20260921_143000.xlsx'
        assert ce.default_filename('csv', kind='groups',
                                   now=__import__('datetime').datetime(2026, 9, 21, 14, 30, 0)
                                   ) == 'groups_20260921_143000.csv'
        assert ce.default_filename('html').endswith('.html')


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------

class TestExportContacts:
    def test_writes_file_and_creates_dir(self, tmp_path, sample):
        out = tmp_path / 'nested' / 'dir' / 'contacts.xlsx'
        res = ce.export_contacts(None, str(out), fmt='xlsx', contacts=sample)
        assert os.path.isfile(out)
        assert res['count'] == len(sample)
        assert res['format'] == 'xlsx'
        assert os.path.getsize(out) > 0

    def test_filters_applied(self, tmp_path, sample):
        out = tmp_path / 'g.csv'
        res = ce.export_contacts(None, str(out), fmt='csv',
                                 contacts=sample, kind='groups')
        assert res['count'] == 2
        rows = list(csv.reader(io.StringIO(out.read_text(encoding='utf-8-sig'))))
        assert all(r[12] == 'Y' for r in rows[1:])

    def test_returns_printable_summary(self, tmp_path, sample):
        lines = []
        ce.export_contacts(None, str(tmp_path / 'a.csv'), fmt='csv',
                           contacts=sample, print_fn=lines.append)
        assert any('5' in ln for ln in lines)


# --------------------------------------------------------------------------
# 脏数据（真实通讯录里存在，合成数据容易漏）
# --------------------------------------------------------------------------

class TestDirtyData:
    """本机 24439 条真实通讯录用 xlsx 导出时崩过：签名/描述里有 XML 非法控制字符。

    openpyxl 写单元格遇到 `\\x0b` 这类字符会抛 IllegalCharacterError，
    CSV / HTML 却没事 —— 所以只有真的跑一遍真实数据的 xlsx 才会发现。
    """

    DIRTY = '\x00\x0b\x0c\x1f'          # XML 1.0 不允许的控制字符
    SURROGATE = '\ud800'                # 落单代理字符，utf-8 编码会炸
    LONG = '长' * 40000

    def _dirty_contact(self):
        return _contact(wxid='wxid_dirty', display_name='脏' + self.DIRTY + '数据',
                        remark='备注' + self.DIRTY, signature=self.SURROGATE + '签名',
                        description=self.LONG)

    def test_xlsx_does_not_crash(self):
        pytest.importorskip('openpyxl')
        data = ce.render_xlsx([self._dirty_contact()])
        assert len(data) > 0

    def test_xlsx_content_is_clean(self):
        openpyxl = pytest.importorskip('openpyxl')
        ws = openpyxl.load_workbook(io.BytesIO(ce.render_xlsx([self._dirty_contact()]))).active
        for row in ws.iter_rows(values_only=True):
            for value in row:
                if isinstance(value, str):
                    assert ce._ILLEGAL_XML_RE.search(value) is None
                    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)

    def test_html_does_not_crash(self):
        html = ce.render_html([self._dirty_contact()])
        assert isinstance(html, str)
        assert len(html.encode('utf-8')) > 0     # 落单代理字符已清掉才能编码

    def test_csv_does_not_crash(self):
        data = ce.render_csv([self._dirty_contact()])
        assert data.startswith(b'\xef\xbb\xbf')
        assert self.DIRTY.encode() not in data

    @pytest.mark.parametrize('fmt', ['csv', 'xlsx', 'html'])
    def test_all_formats_survive(self, fmt):
        assert len(ce.render([self._dirty_contact()], fmt)) > 0

    def test_clean_text_truncates_to_excel_limit(self):
        out = ce.clean_text('x' * 40000)
        assert len(out) == ce.MAX_CELL_CHARS

    def test_clean_text_keeps_normal_text(self):
        assert ce.clean_text('正常中文 mixed ASCII 123') == '正常中文 mixed ASCII 123'
        assert ce.clean_text(1200) == 1200          # 非字符串原样返回
        assert ce.clean_text(None) is None

    def test_visible_emoji_preserved(self):
        """表情/特殊符号不是控制字符，不能误删。"""
        assert ce.clean_text('你好😀 world') == '你好😀 world'


# --------------------------------------------------------------------------
# 筛选逻辑（列表接口与导出共用）
# --------------------------------------------------------------------------

class TestFilterContacts:
    def test_kind_all_keeps_everything(self, sample):
        assert len(filter_contacts(sample, kind='all')) == 5

    def test_kind_contacts_excludes_groups(self, sample):
        got = filter_contacts(sample, kind='contacts')
        assert len(got) == 3
        assert not any(c['is_group'] for c in got)

    def test_kind_groups_only_groups(self, sample):
        got = filter_contacts(sample, kind='groups')
        assert len(got) == 2
        assert all(c['is_group'] for c in got)

    def test_query_matches_multiple_fields(self, sample):
        assert len(filter_contacts(sample, q='李老师')) == 1
        assert len(filter_contacts(sample, q='lisi')) == 1          # alias
        assert len(filter_contacts(sample, q='wxid_beta')) == 1     # wxid
        assert len(filter_contacts(sample, q='13800001111')) == 1   # phone
        assert len(filter_contacts(sample, q='同事')) == 1          # description
        assert len(filter_contacts(sample, q='不存在的人')) == 0

    def test_query_does_not_search_signature(self, sample):
        """沿用旧列表接口的检索字段集（不含 signature/region），避免悄悄改变筛选结果。"""
        assert filter_contacts(sample, q='静水流深') == []

    def test_query_is_case_insensitive(self, sample):
        assert len(filter_contacts(sample, q='WXID_ALPHA')) == 1

    def test_has_chat_filter(self, sample):
        with_chat = filter_contacts(sample, has_chat='1')
        without = filter_contacts(sample, has_chat='0')
        assert all(c['msg_count'] > 0 for c in with_chat)
        assert all(c['msg_count'] == 0 for c in without)
        assert len(with_chat) + len(without) == len(sample)

    def test_letter_filter(self, sample):
        got = filter_contacts(sample, letter='李')
        assert [c['display_name'] for c in got] == ['李老师']

    def test_sort_by_msg_count(self, sample):
        got = filter_contacts(sample, sort='msg_count')
        counts = [c['msg_count'] for c in got]
        assert counts == sorted(counts, reverse=True)

    def test_sort_by_last_time(self, sample):
        got = filter_contacts(sample, sort='last_time')
        times = [c['last_msg_time'] or 0 for c in got]
        assert times == sorted(times, reverse=True)

    def test_default_sort_is_name(self, sample):
        got = filter_contacts(sample, sort='name')
        names = [(c['display_name'] or c['wxid']).lower() for c in got]
        assert names == sorted(names)

    def test_filters_combine(self, sample):
        got = filter_contacts(sample, kind='groups', has_chat='1')
        assert [c['display_name'] for c in got] == ['相亲相爱一家人']

    def test_does_not_mutate_input(self, sample):
        before = [c['wxid'] for c in sample]
        filter_contacts(sample, sort='msg_count')
        assert [c['wxid'] for c in sample] == before
