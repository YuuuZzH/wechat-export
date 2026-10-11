"""通讯录加载层：把 extra_buffer 的字段并进联系人，以及按标签筛选。

两条加载路径都必须拿到这些字段：
  - slow path：直接读 contact.db（有 extra_buffer）
  - fast path：读项目自建的 data/chats.db `contacts` 表 —— **该表没有 extra_buffer**，
    必须回源 contact.db 才拿得到手机号/标签，这是最容易漏的一点。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import blackboxprotobuf  # noqa: F401  (确保依赖存在，与其他用例一致)

from engine.services import address_book
from engine.services.contact_extra import parse_extra_buffer


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _ld(fno, payload):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    return _varint((fno << 3) | 2) + _varint(len(payload)) + payload


def _vint(fno, value):
    return _varint((fno << 3) | 0) + _varint(value)


def buf(*, sex=None, signature=None, country=None, province=None, city=None,
        phone=None, labels=None):
    parts = []
    if sex is not None:
        parts.append(_vint(2, sex))
    if signature is not None:
        parts.append(_ld(4, signature))
    if country is not None:
        parts.append(_ld(5, country))
    if province is not None:
        parts.append(_ld(6, province))
    if city is not None:
        parts.append(_ld(7, city))
    if phone is not None:
        parts.append(_ld(14, _vint(1, 1) + _ld(2, _ld(1, phone))))
    if labels is not None:
        parts.append(_ld(30, labels))
    return b''.join(parts)


LABELS = [(5, 'only_work', 4), (4, 'non_work', 3), (6, '家人', 9)]

ROWS = [
    # id, username, remark, nick, alias, extra_buffer
    (1, 'wxid_phone', '华为-张佩雯', 'ZPP', 'zp1',
     buf(sex=2, signature='the best people in life are free✨', country='CN',
         province='Shaanxi', city="Xi'an", phone='13800000001', labels='5')),
    (2, 'wxid_multi', '李四', 'LS', 'ls1', buf(sex=1, labels='4,5')),
    (3, 'wxid_plain', '王五', 'WW', 'ww1', b''),
    (4, 'wxid_unknown_label', '赵六', 'ZL', 'zl1', buf(labels='99')),
    (5, '88888888@chatroom', '工作群', '', '', buf(sex=0, labels='6')),
]


def _write_contact_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE contact (id INTEGER, username TEXT, remark TEXT,'
                ' nick_name TEXT, alias TEXT, extra_buffer BLOB)')
    con.executemany('INSERT INTO contact VALUES (?,?,?,?,?,?)', ROWS)
    con.execute('CREATE TABLE contact_label (label_id_ INTEGER, label_name_ TEXT,'
                ' sort_order_ INTEGER)')
    con.executemany('INSERT INTO contact_label VALUES (?,?,?)', LABELS)
    con.commit()
    con.close()


@pytest.fixture
def decrypted_slow(tmp_path):
    """只有 contact.db —— 走 slow path。"""
    d = tmp_path / 'slow'
    _write_contact_db(str(d / 'contact' / 'contact.db'))
    return str(d)


@pytest.fixture
def decrypted_fast(tmp_path):
    """带 data/chats.db 索引 —— 走 fast path（索引里没有 extra_buffer）。"""
    d = tmp_path / 'fast'
    _write_contact_db(str(d / 'contact' / 'contact.db'))
    os.makedirs(str(d / 'data'), exist_ok=True)
    con = sqlite3.connect(str(d / 'data' / 'chats.db'))
    con.execute('CREATE TABLE contacts (wxid TEXT, display_name TEXT, remark TEXT,'
                ' nick_name TEXT, alias TEXT, is_group INTEGER)')
    con.executemany('INSERT INTO contacts VALUES (?,?,?,?,?,?)', [
        ('wxid_phone', '华为-张佩雯', '华为-张佩雯', 'ZPP', 'zp1', 0),
        ('wxid_multi', '李四', '李四', 'LS', 'ls1', 0),
        ('wxid_plain', '王五', '王五', 'WW', 'ww1', 0),
        ('wxid_unknown_label', '赵六', '赵六', 'ZL', 'zl1', 0),
        ('88888888@chatroom', '工作群', '', '', '', 1),
    ])
    con.execute('CREATE TABLE chats (chat_id TEXT, message_count INTEGER,'
                ' last_msg_time INTEGER)')
    con.executemany('INSERT INTO chats VALUES (?,?,?)', [
        ('wxid_phone', 120, 1789531971),
        ('wxid_multi', 7, 1700000000),
        ('wxid_plain', 0, None),
        ('wxid_unknown_label', 0, None),
        ('88888888@chatroom', 99, 1789600000),
    ])
    con.commit()
    con.close()
    return str(d)


def _by_wxid(contacts):
    return {c['wxid']: c for c in contacts}


# --------------------------------------------------------------------------
# 字段合并：两条路径都要有
# --------------------------------------------------------------------------

class TestFieldMerge:
    @pytest.mark.parametrize('fixture_name', ['decrypted_slow', 'decrypted_fast'])
    def test_phone_from_extra_buffer(self, request, fixture_name):
        address_book._ALL_CONTACTS_CACHE.clear()
        c = _by_wxid(address_book.get_all_contacts(request.getfixturevalue(fixture_name)))
        assert c['wxid_phone']['phone'] == '13800000001'

    @pytest.mark.parametrize('fixture_name', ['decrypted_slow', 'decrypted_fast'])
    def test_sex(self, request, fixture_name):
        address_book._ALL_CONTACTS_CACHE.clear()
        c = _by_wxid(address_book.get_all_contacts(request.getfixturevalue(fixture_name)))
        assert c['wxid_phone']['sex'] == 2
        assert c['wxid_multi']['sex'] == 1

    @pytest.mark.parametrize('fixture_name', ['decrypted_slow', 'decrypted_fast'])
    def test_signature_and_region(self, request, fixture_name):
        address_book._ALL_CONTACTS_CACHE.clear()
        c = _by_wxid(address_book.get_all_contacts(request.getfixturevalue(fixture_name)))
        c1 = c['wxid_phone']
        assert c1['signature'] == 'the best people in life are free✨'
        assert c1['country'] == 'CN'
        assert c1['province'] == 'Shaanxi'
        assert c1['city'] == "Xi'an"

    @pytest.mark.parametrize('fixture_name', ['decrypted_slow', 'decrypted_fast'])
    def test_labels_resolved_to_names(self, request, fixture_name):
        address_book._ALL_CONTACTS_CACHE.clear()
        c = _by_wxid(address_book.get_all_contacts(request.getfixturevalue(fixture_name)))
        assert c['wxid_phone']['labels'] == ['only_work']
        assert c['wxid_phone']['label_ids'] == [5]
        assert c['wxid_multi']['labels'] == ['non_work', 'only_work']
        assert c['88888888@chatroom']['labels'] == ['家人']

    @pytest.mark.parametrize('fixture_name', ['decrypted_slow', 'decrypted_fast'])
    def test_contact_without_extra_has_empty_labels(self, request, fixture_name):
        address_book._ALL_CONTACTS_CACHE.clear()
        c = _by_wxid(address_book.get_all_contacts(request.getfixturevalue(fixture_name)))
        assert c['wxid_plain']['labels'] == []
        assert 'phone' not in c['wxid_plain']

    @pytest.mark.parametrize('fixture_name', ['decrypted_slow', 'decrypted_fast'])
    def test_unknown_label_id_is_not_invented(self, request, fixture_name):
        address_book._ALL_CONTACTS_CACHE.clear()
        c = _by_wxid(address_book.get_all_contacts(request.getfixturevalue(fixture_name)))
        assert c['wxid_unknown_label']['labels'] == []
        assert c['wxid_unknown_label']['label_ids'] == [99]

    def test_phone_source_recorded(self, decrypted_slow):
        """真实号码应覆盖"从 wxid 猜号码"，并标注来源。"""
        address_book._ALL_CONTACTS_CACHE.clear()
        c = _by_wxid(address_book.get_all_contacts(decrypted_slow))
        assert c['wxid_phone']['phone_source'] == 'contact_db'

    def test_phone_from_wxid_still_works_when_no_real_phone(self, tmp_path):
        """没有真实号码时保留原有的 wxid 猜号能力（旧行为不退化）。"""
        d = tmp_path / 'wxidphone'
        p = str(d / 'contact' / 'contact.db')
        os.makedirs(os.path.dirname(p), exist_ok=True)
        con = sqlite3.connect(p)
        con.execute('CREATE TABLE contact (id INTEGER, username TEXT, remark TEXT,'
                    ' nick_name TEXT, alias TEXT, extra_buffer BLOB)')
        con.execute('INSERT INTO contact VALUES (1,?,?,?,?,?)',
                    ('+8613812345678', '手机号用户', '', '', b''))
        con.commit()
        con.close()
        address_book._ALL_CONTACTS_CACHE.clear()
        c = _by_wxid(address_book.get_all_contacts(str(d)))
        # 不锁定既有正则的分组细节（贪婪匹配到 861），只确认兜底链路仍生效
        assert c['+8613812345678']['phone'].startswith('+86')
        assert c['+8613812345678']['phone_source'] == 'wxid'

    def test_real_phone_overrides_wxid_guess(self, tmp_path):
        """wxid 看着像手机号、extra_buffer 里也有真号时，以真号为准。"""
        d = tmp_path / 'both'
        p = str(d / 'contact' / 'contact.db')
        os.makedirs(os.path.dirname(p), exist_ok=True)
        con = sqlite3.connect(p)
        con.execute('CREATE TABLE contact (id INTEGER, username TEXT, remark TEXT,'
                    ' nick_name TEXT, alias TEXT, extra_buffer BLOB)')
        con.execute('INSERT INTO contact VALUES (1,?,?,?,?,?)',
                    ('+8613100000000', '某人', '', '', buf(phone='13800000001')))
        con.commit()
        con.close()
        address_book._ALL_CONTACTS_CACHE.clear()
        c = _by_wxid(address_book.get_all_contacts(str(d)))
        assert c['+8613100000000']['phone'] == '13800000001'
        assert c['+8613100000000']['phone_source'] == 'contact_db'


# --------------------------------------------------------------------------
# 按标签筛选
# --------------------------------------------------------------------------

class TestLabelFilter:
    @pytest.fixture
    def contacts(self, decrypted_slow):
        address_book._ALL_CONTACTS_CACHE.clear()
        return address_book.get_all_contacts(decrypted_slow)

    def test_filter_by_label_name(self, contacts):
        got = address_book.filter_contacts(contacts, label='only_work')
        assert sorted(c['wxid'] for c in got) == ['wxid_multi', 'wxid_phone']

    def test_filter_single_match(self, contacts):
        got = address_book.filter_contacts(contacts, label='non_work')
        assert [c['wxid'] for c in got] == ['wxid_multi']

    def test_filter_is_case_insensitive(self, contacts):
        assert len(address_book.filter_contacts(contacts, label='ONLY_WORK')) == 2

    def test_filter_chinese_label(self, contacts):
        got = address_book.filter_contacts(contacts, label='家人')
        assert [c['wxid'] for c in got] == ['88888888@chatroom']

    def test_no_match_returns_empty(self, contacts):
        assert address_book.filter_contacts(contacts, label='不存在') == []

    def test_empty_label_means_no_filter(self, contacts):
        assert len(address_book.filter_contacts(contacts, label='')) == len(contacts)
        assert len(address_book.filter_contacts(contacts, label=None)) == len(contacts)

    def test_label_combines_with_other_filters(self, decrypted_fast):
        # 需要 chats.db 才有 msg_count，因此这里用 fast fixture
        address_book._ALL_CONTACTS_CACHE.clear()
        contacts = address_book.get_all_contacts(decrypted_fast)
        got = address_book.filter_contacts(contacts, label='non_work', has_chat='1')
        assert [c['wxid'] for c in got] == ['wxid_multi']

    def test_distinct_labels_helper(self, contacts):
        assert address_book.distinct_labels(contacts) == ['non_work', 'only_work', '家人']
