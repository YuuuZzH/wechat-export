"""`contact.extra_buffer` protobuf 解析测试（合成字节，不依赖真实微信数据）。

背景：联系人手机号/性别/签名/国家省市/标签**都不在 contact 表的列里**，
而在 `contact.extra_buffer` 这个 protobuf blob 中。旧代码 `_KNOWN_EXTRA_COLS`
声明了 sex/country/province/city/signature 五个列，但 contact 表没有这些列，
所以这些字段一直是空的。

字段地图（在 24,431 条真实联系人上验证）：
    2       性别     0=未知 1=男 2=女
    4       个性签名 自由文本（少数人直接填手机号）
    5/6/7   国家/省/市
    14.2.1  手机号
    30      标签 id 串，逗号分隔，如 '4,5'

**必须用确定性 wire-format 遍历，不能用 blackboxprotobuf**：
实测全量差分 68/22,203 条不一致，全部是 blackboxprotobuf 的类型猜测失败 ——
它会把 11 位 ASCII 手机号 `'15555555581'` 当成嵌套 message
（解成 `{"6":…,"7":49}`），手机号因此丢失。见 TestBlackboxProtobufRegression。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from engine.services import contact_extra as ce


# --------------------------------------------------------------------------
# 最小 protobuf 编码器（测试用）
# --------------------------------------------------------------------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(fno: int, wt: int) -> bytes:
    return _varint((fno << 3) | wt)


def ld(fno: int, payload) -> bytes:
    """length-delimited（嵌套 message 或字符串/bytes）"""
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    return _tag(fno, 2) + _varint(len(payload)) + payload


def vint(fno: int, value: int) -> bytes:
    """varint 整数"""
    return _tag(fno, 0) + _varint(value)


def make_buffer(*, sex=None, signature=None, country=None, province=None,
                city=None, phone=None, phone_type=1, labels=None,
                extra=b'') -> bytes:
    """按字段地图拼一个 extra_buffer。"""
    parts = [extra]
    if sex is not None:
        parts.append(vint(2, sex))
    if signature is not None:
        parts.append(ld(4, signature))
    if country is not None:
        parts.append(ld(5, country))
    if province is not None:
        parts.append(ld(6, province))
    if city is not None:
        parts.append(ld(7, city))
    if phone is not None:
        parts.append(ld(14, vint(1, phone_type) + ld(2, ld(1, phone))))
    if labels is not None:
        parts.append(ld(30, labels))
    return b''.join(parts)


# --------------------------------------------------------------------------
# 字段提取
# --------------------------------------------------------------------------

class TestParseFields:
    def test_phone_from_nested_field(self):
        got = ce.parse_extra_buffer(make_buffer(phone='13800000001'))
        assert got['phone'] == '13800000001'

    def test_signature(self):
        got = ce.parse_extra_buffer(make_buffer(signature='the best people in life are free✨'))
        assert got['signature'] == 'the best people in life are free✨'

    def test_region(self):
        got = ce.parse_extra_buffer(make_buffer(country='CN', province='Shaanxi', city="Xi'an"))
        assert got['country'] == 'CN'
        assert got['province'] == 'Shaanxi'
        assert got['city'] == "Xi'an"

    def test_sex_values(self):
        for raw in (0, 1, 2):
            assert ce.parse_extra_buffer(make_buffer(sex=raw))['sex'] == raw

    def test_all_fields_together(self):
        got = ce.parse_extra_buffer(make_buffer(
            sex=2, signature='签名', country='CN', province='Shaanxi',
            city="Xi'an", phone='13800000001', labels='5'))
        assert got == {
            'sex': 2,
            'signature': '签名',
            'country': 'CN',
            'province': 'Shaanxi',
            'city': "Xi'an",
            'phone': '13800000001',
            'label_ids': [5],
        }

    def test_empty_region_fields_skipped(self):
        """真实数据里省/市常是长度为 0 的空串，不应产生空字段。"""
        got = ce.parse_extra_buffer(make_buffer(country='CN', province='', city=''))
        assert got.get('country') == 'CN'
        assert 'province' not in got
        assert 'city' not in got


class TestLabels:
    def test_single_label(self):
        assert ce.parse_extra_buffer(make_buffer(labels='5'))['label_ids'] == [5]

    def test_multiple_labels(self):
        got = ce.parse_extra_buffer(make_buffer(labels='4,5'))
        assert got['label_ids'] == [4, 5]

    def test_four_labels(self):
        got = ce.parse_extra_buffer(make_buffer(labels='4,10,11,6'))
        assert got['label_ids'] == [4, 10, 11, 6]

    def test_tolerates_spaces_and_trailing_comma(self):
        got = ce.parse_extra_buffer(make_buffer(labels=' 4 , 5 ,'))
        assert got['label_ids'] == [4, 5]

    def test_empty_labels_string(self):
        assert 'label_ids' not in ce.parse_extra_buffer(make_buffer(labels=''))

    def test_non_numeric_label_ignored(self):
        assert 'label_ids' not in ce.parse_extra_buffer(make_buffer(labels='abc'))

    def test_mixed_numeric_and_garbage(self):
        """含非数字时整条丢弃，避免产出半个标签集。"""
        got = ce.parse_extra_buffer(make_buffer(labels='4,abc'))
        assert 'label_ids' not in got


class TestPhoneFallback:
    def test_phone_in_signature_field_used_as_fallback(self):
        """实测有 12 个联系人把手机号直接填在 field 4。"""
        got = ce.parse_extra_buffer(make_buffer(signature='15555555581'))
        assert got['phone'] == '15555555581'
        assert got['phone_from_signature'] is True

    def test_real_phone_wins_over_signature_fallback(self):
        got = ce.parse_extra_buffer(make_buffer(signature='13100000000',
                                                phone='13800000001'))
        assert got['phone'] == '13800000001'
        assert not got.get('phone_from_signature')

    def test_signature_that_is_not_a_phone_is_not_used(self):
        got = ce.parse_extra_buffer(make_buffer(signature='ender'))
        assert 'phone' not in got
        assert got['signature'] == 'ender'

    def test_non_mobile_number_not_treated_as_phone(self):
        # 10086 / 座机 不应被当成手机号
        assert 'phone' not in ce.parse_extra_buffer(make_buffer(signature='10086'))
        assert 'phone' not in ce.parse_extra_buffer(make_buffer(signature='010-12345678'))


class TestRobustness:
    def test_empty_buffer(self):
        assert ce.parse_extra_buffer(b'') == {}
        assert ce.parse_extra_buffer(None) == {}

    def test_garbage_buffer_does_not_raise(self):
        for bad in (b'\xff\xff\xff\xff', b'\x0a', b'\x08\xff', os.urandom(64)):
            assert isinstance(ce.parse_extra_buffer(bad), dict)

    def test_truncated_length_delimited(self):
        buf = _tag(4, 2) + _varint(50) + b'short'
        assert isinstance(ce.parse_extra_buffer(buf), dict)

    def test_non_utf8_text_field_ignored(self):
        buf = ld(4, b'\xff\xfe\x00\x01')
        got = ce.parse_extra_buffer(buf)
        assert 'signature' not in got

    def test_partial_parse_keeps_earlier_fields(self):
        """坏数据出现在后半段时，前半段已解析出的字段不应丢失。"""
        buf = ld(4, 'good') + b'\xff\xff\xff\xff'
        got = ce.parse_extra_buffer(buf)
        assert got.get('signature') == 'good'

    def test_field_number_zero_is_tolerated(self):
        assert isinstance(ce.parse_extra_buffer(b'\x00\x00'), dict)

    def test_large_varint(self):
        buf = vint(2, 2) + vint(41, 1686743180)
        assert ce.parse_extra_buffer(buf)['sex'] == 2


class TestBlackboxProtobufRegression:
    """blackboxprotobuf 会把这几个 blob 解错，本模块必须解对。"""

    BBP_FAILING_PHONE = '15555555581'

    def test_phone_that_blackboxprotobuf_misparses(self):
        """该号码是 11 位 ASCII，其字节恰好构成合法 protobuf，bbp 会当嵌套 message。"""
        buf = make_buffer(signature='深耕教培领域，定制学习方案', country='CN',
                          province='', city='', phone=self.BBP_FAILING_PHONE)
        got = ce.parse_extra_buffer(buf)
        assert got['phone'] == self.BBP_FAILING_PHONE
        assert got['signature'] == '深耕教培领域，定制学习方案'

    def test_signature_that_looks_like_protobuf(self):
        """'ender' 曾被 bbp 解成 {"12": 1919247470}。"""
        buf = make_buffer(signature='ender', country='CN',
                          province='Sichuan', city='Chengdu', labels='5')
        got = ce.parse_extra_buffer(buf)
        assert got['signature'] == 'ender'
        assert got['province'] == 'Sichuan'
        assert got['city'] == 'Chengdu'

    def test_city_that_looks_like_protobuf(self):
        """'Al Jubail' / 'HK' 曾被 bbp 解错。"""
        got = ce.parse_extra_buffer(make_buffer(province='Al Jubail'))
        assert got['province'] == 'Al Jubail'
        assert ce.parse_extra_buffer(make_buffer(country='HK'))['country'] == 'HK'


# --------------------------------------------------------------------------
# 标签表
# --------------------------------------------------------------------------

@pytest.fixture
def label_db(tmp_path):
    p = tmp_path / 'contact.db'
    con = sqlite3.connect(p)
    con.execute('CREATE TABLE contact_label (label_id_ INTEGER, label_name_ TEXT, sort_order_ INTEGER)')
    con.executemany('INSERT INTO contact_label VALUES (?,?,?)', [
        (5, 'only_work', 4),
        (4, 'non_work', 3),
        (1, 'private_home', 1),
        (6, '家人', 9),
    ])
    con.commit()
    con.close()
    return str(p)


class TestLoadLabels:
    def test_returns_id_to_name(self, label_db):
        labels = ce.load_labels(label_db)
        assert labels[5] == 'only_work'
        assert labels[6] == '家人'

    def test_missing_table_returns_empty(self, tmp_path):
        p = tmp_path / 'empty.db'
        sqlite3.connect(p).close()
        assert ce.load_labels(str(p)) == {}

    def test_missing_file_returns_empty(self, tmp_path):
        assert ce.load_labels(str(tmp_path / 'nope.db')) == {}

    def test_none_returns_empty(self):
        assert ce.load_labels(None) == {}

    def test_resolve_ids_to_names(self, label_db):
        labels = ce.load_labels(label_db)
        assert ce.resolve_labels([4, 5], labels) == ['non_work', 'only_work']
        assert ce.resolve_labels([5], labels) == ['only_work']

    def test_resolve_keeps_unknown_ids_out_of_names(self, label_db):
        labels = ce.load_labels(label_db)
        # 99 不在表里：名称跳过，但不能崩
        assert ce.resolve_labels([5, 99], labels) == ['only_work']

    def test_resolve_empty(self, label_db):
        assert ce.resolve_labels([], ce.load_labels(label_db)) == []
        assert ce.resolve_labels(None, {}) == []


# --------------------------------------------------------------------------
# 批量加载
# --------------------------------------------------------------------------

@pytest.fixture
def contact_db(tmp_path):
    """最小 contact.db：含真实列 + extra_buffer。"""
    p = tmp_path / 'contact.db'
    con = sqlite3.connect(p)
    con.execute('CREATE TABLE contact (id INTEGER, username TEXT, remark TEXT,'
                ' nick_name TEXT, alias TEXT, extra_buffer BLOB)')
    con.executemany('INSERT INTO contact VALUES (?,?,?,?,?,?)', [
        (1, 'wxid_with_phone', '华为-张三', 'ZP', 'zp1',
         make_buffer(sex=2, signature='签名A', country='CN', province='Shaanxi',
                     city="Xi'an", phone='13800000001', labels='5')),
        (2, 'wxid_multi_label', '李四', 'LS', 'ls1',
         make_buffer(sex=1, labels='4,5')),
        (3, 'wxid_empty', '王五', 'WW', 'ww1', b''),
        (4, 'wxid_null', '赵六', 'ZL', 'zl1', None),
        (5, '12345@chatroom', '群', '', '', make_buffer(sex=0)),
    ])
    con.execute('CREATE TABLE contact_label (label_id_ INTEGER, label_name_ TEXT, sort_order_ INTEGER)')
    con.executemany('INSERT INTO contact_label VALUES (?,?,?)',
                    [(5, 'only_work', 4), (4, 'non_work', 3)])
    con.commit()
    con.close()
    return str(p)


class TestLoadExtraMap:
    def test_keyed_by_username(self, contact_db):
        m = ce.load_extra_map(contact_db)
        assert m['wxid_with_phone']['phone'] == '13800000001'
        assert m['wxid_multi_label']['label_ids'] == [4, 5]

    def test_empty_and_null_buffers_omitted(self, contact_db):
        m = ce.load_extra_map(contact_db)
        assert 'wxid_empty' not in m
        assert 'wxid_null' not in m

    def test_missing_file_returns_empty(self, tmp_path):
        assert ce.load_extra_map(str(tmp_path / 'nope.db')) == {}

    def test_all_rows_scanned(self, contact_db):
        m = ce.load_extra_map(contact_db)
        # 5 个联系人中 3 个有内容
        assert len(m) == 3


class TestSexLabels:
    def test_label_map(self):
        assert ce.sex_label(1) == '男'
        assert ce.sex_label(2) == '女'
        assert ce.sex_label(0) == ''
        assert ce.sex_label(None) == ''
        assert ce.sex_label('2') == '女'
        assert ce.sex_label(9) == ''
