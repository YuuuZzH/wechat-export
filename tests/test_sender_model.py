# -*- coding: utf-8 -*-
"""发送者归属：**唯一权威实现**（`engine/services/sender_model.py`）的测试。

背景（issue #16 / `todo.txt` #3/#4/#8/#17–#20/#27/#30）
------------------------------------------------------
微信 4.x 的 `Msg_<md5(chat_id)>` 表里**没有"谁发的"这一列**，历史上项目里有四套
并行推断，导致同一批数据在气泡 / 发送者筛选 / 统计 / 搜索里结论不一致。

权威判据是**分片级 `Name2Id` 表**：`real_sender_id` 就是它的 `rowid`。
控制方在本机真实数据上的验证（见会话报告）：

* 与独立内容证据（`fromusername` / 正文前缀）交叉验证 **234,299 行**
  → 一致 **234,196 行 = 99.956%**；
* **单聊内部一致性**（不依赖内容证据）：200 个单聊表 / 148,628 行，
  解析出的名字**只落在 {本人, chat_id}，第三方 0 行**；
* **分片级 rsid 共识**（用群聊前缀当裁判）：7,889 个有共识的 rsid 里，
  与 `Name2Id` 冲突的**强共识 = 0**；
* 同真值对照（178,759 行）：**旧规则错判 1.13% → 本方法 0.056%**。

本文件全部**合成**，不读任何真实数据。
"""
import hashlib
import os
import sqlite3
import struct

import pytest

from engine.services import message as M
from engine.services.sender_model import (ShardSenderModel, content_sender_evidence,
                                          load_name2id, own_id_forms,
                                          SIDE_ME, SIDE_OTHER, SIDE_SYSTEM, SIDE_UNKNOWN,
                                          SOURCE_NAME2ID, SOURCE_FROMUSERNAME,
                                          SOURCE_PREFIX, SOURCE_ORIGIN, SOURCE_SYSTEM,
                                          SOURCE_NONE)

CHAT = 'wxid_friend_1a2b'
OWN_DIR = 'wxid_me12cd34ef56_9f2c'      # `app.config['WXID']` 实测就是账号目录名
OWN_BARE = 'wxid_me12cd34ef56'
MEMBER = 'wxid_member_3c4d'


def msg_xml(fromusername, extra=''):
    return ('<msg><appmsg><title>t</title></appmsg>'
            '<fromusername>%s</fromusername>%s</msg>' % (fromusername, extra))


# ---------------------------------------------------------------------------
# A. 模型本身
# ---------------------------------------------------------------------------

class TestOwnIdForms:
    def test_covers_dir_name_and_bare_id(self):
        forms = own_id_forms(OWN_DIR)
        assert OWN_DIR in forms and OWN_BARE in forms

    def test_covers_custom_alias(self):
        forms = own_id_forms('myalias_68f8')
        assert 'myalias' in forms and 'myalias_68f8' in forms


class TestName2IdIsAuthoritative:
    """`Name2Id` 能给出答案时，**不许**被内容证据或默认值推翻。"""

    def _model(self, chat_id=CHAT):
        return ShardSenderModel({1: CHAT, 2: OWN_BARE, 3: MEMBER, 4: 'wxid_someone'},
                               chat_id, OWN_DIR)

    def test_own_rsid_is_me(self):
        side, src, member = self._model().classify(2, 0, 'anything')
        assert (side, src) == (SIDE_ME, SOURCE_NAME2ID)
        assert member == OWN_BARE

    def test_counterpart_rsid_is_other(self):
        side, src, member = self._model().classify(1, 0, None)
        assert (side, src, member) == (SIDE_OTHER, SOURCE_NAME2ID, CHAT)

    def test_image_without_any_content_evidence_still_resolves(self):
        """核心场景：图片没有前缀、没有 fromusername —— 以前只能"猜"，现在有权威答案。"""
        side, src, _ = self._model().classify(1, 10, None, local_type=3)
        assert (side, src) == (SIDE_OTHER, SOURCE_NAME2ID)

    def test_name2id_wins_over_content_evidence(self):
        """内容证据被"转发"污染时（payload 保留原作者），以 Name2Id 为准。"""
        side, src, _ = self._model().classify(2, 10, msg_xml(CHAT))
        assert (side, src) == (SIDE_ME, SOURCE_NAME2ID)

    def test_group_member_is_other_with_member_id(self):
        model = ShardSenderModel({2: OWN_BARE, 3: MEMBER}, 'room@chatroom', OWN_DIR)
        side, src, member = model.classify(3, 10, '<msg/>')
        assert (side, src, member) == (SIDE_OTHER, SOURCE_NAME2ID, MEMBER)

    def test_group_own_message_is_me(self):
        model = ShardSenderModel({2: OWN_BARE}, 'room@chatroom', OWN_DIR)
        assert model.classify(2, 10, None)[:2] == (SIDE_ME, SOURCE_NAME2ID)

    def test_single_chat_third_party_is_reported_as_other_with_that_id(self):
        """单聊里指向第三方（本机 0 例，防御性）：仍然算"对方"，但 member 交出去。"""
        side, src, member = self._model().classify(4, 10, None)
        assert (side, src, member) == (SIDE_OTHER, SOURCE_NAME2ID, 'wxid_someone')


class TestFallbacksWhenName2IdIsMissing:
    """`Name2Id` 缺失（本机 1.86%，其中 99% 是系统消息）时才走这些档。"""

    def _model(self, chat_id=CHAT, n2i=None):
        return ShardSenderModel(n2i or {9: 'wxid_unrelated'}, chat_id, OWN_DIR)

    def test_fromusername_equal_to_chat_is_other(self):
        side, src, _ = self._model().classify(4, 10, msg_xml(CHAT))
        assert (side, src) == (SIDE_OTHER, SOURCE_FROMUSERNAME)

    def test_fromusername_equal_to_own_is_me(self):
        side, src, _ = self._model().classify(4, 10, msg_xml(OWN_BARE))
        assert (side, src) == (SIDE_ME, SOURCE_FROMUSERNAME)

    def test_quoted_sender_does_not_count(self):
        """引用块里的 fromusername 是**被引用那条**的发送者，不能当证据。"""
        content = ('<msg><appmsg><refermsg><fromusername>%s</fromusername></refermsg>'
                   '</appmsg><fromusername>%s</fromusername></msg>' % (CHAT, OWN_BARE))
        side, src, _ = self._model().classify(4, 10, content)
        assert (side, src) == (SIDE_ME, SOURCE_FROMUSERNAME)

    def test_prefix_of_counterpart_is_other(self):
        side, src, _ = self._model().classify(4, 10, '%s:\n你好' % CHAT)
        assert (side, src) == (SIDE_OTHER, SOURCE_PREFIX)

    def test_prefix_of_own_form_is_me(self):
        side, src, _ = self._model().classify(4, 10, '%s:\n你好' % OWN_BARE)
        assert (side, src) == (SIDE_ME, SOURCE_PREFIX)

    def test_origin_one_is_me(self):
        side, src, _ = self._model().classify(4, 1, None)
        assert (side, src) == (SIDE_ME, SOURCE_ORIGIN)

    def test_system_notice_is_system(self):
        """系统提示（撤回/入群…）不属于任何个人 —— 本机实测占缺失行的 99%。"""
        side, src, _ = self._model().classify(4, 10, None, local_type=10000)
        assert (side, src) == (SIDE_SYSTEM, SOURCE_SYSTEM)
        side, src, _ = self._model().classify(4, 2, None, local_type=10002)
        assert (side, src) == (SIDE_SYSTEM, SOURCE_SYSTEM)

    def test_no_signal_is_unknown_not_a_guess(self):
        """什么都没有 ⇒ `unknown`。**绝不**回退成"我发的"（那是历史缺陷的根因）。"""
        side, src, _ = self._model().classify(4, 10, None, local_type=3)
        assert (side, src) == (SIDE_UNKNOWN, SOURCE_NONE)

    def test_rsid_zero_never_matches_name2id_rowid_zero(self):
        model = ShardSenderModel({0: OWN_BARE}, CHAT, OWN_DIR)
        side, src, _ = model.classify(0, 10, None, local_type=3)
        assert side == SIDE_UNKNOWN


class TestContentEvidenceFunction:
    def test_payload_without_fromusername(self):
        assert content_sender_evidence('<msg><appmsg/></msg>', CHAT,
                                       own_id_forms(OWN_DIR)) == (None, SOURCE_NONE)

    def test_both_sides_present_is_undecidable(self):
        content = msg_xml(CHAT, '<fromusername>%s</fromusername>' % OWN_BARE)
        assert content_sender_evidence(content, CHAT, own_id_forms(OWN_DIR))[0] is None

    def test_group_prefixed_member_is_other(self):
        side, src = content_sender_evidence('%s:\nhi' % MEMBER, 'room@chatroom',
                                           own_id_forms(OWN_DIR), is_group=True)
        assert (side, src) == (SIDE_OTHER, SOURCE_PREFIX)


class TestLoadName2Id:
    def test_reads_rowid_to_user_name(self, tmp_path):
        db = tmp_path / 'shard.db'
        conn = sqlite3.connect(str(db))
        conn.execute('CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)')
        conn.execute("INSERT INTO Name2Id VALUES ('wxid_a', 0)")
        conn.execute("INSERT INTO Name2Id VALUES ('wxid_b', 1)")
        conn.commit()
        got = load_name2id(conn)
        conn.close()
        assert got == {1: 'wxid_a', 2: 'wxid_b'}

    def test_missing_table_returns_empty_not_raise(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / 'x.db'))
        assert load_name2id(conn) == {}
        conn.close()


# ---------------------------------------------------------------------------
# B. 端到端：合成一个分片，走真实的 query_messages / get_chat_stats
# ---------------------------------------------------------------------------

def _build_v2_plain_text(text):
    return text.encode('utf-8')


def _make_shard(tmp_path, chat_id=CHAT):
    """造一个含 `Name2Id` + `Msg_<md5(chat)>` 的分片。"""
    dec = tmp_path / 'decrypted'
    mdir = dec / 'message'
    mdir.mkdir(parents=True, exist_ok=True)
    db = mdir / 'message_0.db'
    table = 'Msg_' + hashlib.md5(chat_id.encode()).hexdigest()
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)')
    conn.execute("INSERT INTO Name2Id VALUES (?, 0)", (chat_id,))      # rowid 1 = 对方
    conn.execute("INSERT INTO Name2Id VALUES (?, 0)", (OWN_BARE,))     # rowid 2 = 本人
    conn.execute("INSERT INTO Name2Id VALUES (?, 0)", (MEMBER,))       # rowid 3 = 第三方
    conn.execute(
        'CREATE TABLE [%s] (local_id INTEGER PRIMARY KEY, local_type INTEGER, '
        'origin_source INTEGER, create_time INTEGER, status INTEGER, '
        'message_content BLOB, real_sender_id INTEGER, packed_info_data BLOB)' % table)
    rows = [
        # (local_id, type, origin, ts, content, rsid)
        (1, 1, 10, 1700000001, '%s:\n对方发的文本' % chat_id, 1),   # Name2Id=chat ⇒ other
        (2, 1, 10, 1700000002, '我发的文本', 2),                     # Name2Id=own  ⇒ me
        (3, 3, 10, 1700000003, None, 1),                             # 图片、无任何内容证据
        (4, 3, 10, 1700000004, None, 4),                             # Name2Id 缺失 ⇒ unknown
        (5, 10000, 10, 1700000005, '系统提示', 4),                    # 系统消息
        (6, 1, 1, 1700000006, 'origin=1 的自发消息', 4),              # 兜底：origin
    ]
    conn.executemany(
        'INSERT INTO [%s] (local_id, local_type, origin_source, create_time, status, '
        'message_content, real_sender_id, packed_info_data) VALUES (?,?,?,?,3,?,?,NULL)'
        % table, rows)
    conn.commit()
    conn.close()
    return str(dec), table


class TestQueryMessagesUsesTheModel:
    def test_every_row_gets_the_right_side(self, tmp_path):
        dec, _t = _make_shard(tmp_path)
        res = M.query_messages(dec, CHAT, wxid=OWN_DIR, per_page=50)
        by_id = {m['id']: m for m in res['messages']}

        assert by_id[1]['sender_side'] == SIDE_OTHER
        assert by_id[1]['sender_evidence'] == SOURCE_NAME2ID
        assert by_id[2]['sender_side'] == SIDE_ME and by_id[2]['is_sender'] is True
        assert by_id[3]['sender_side'] == SIDE_OTHER, '图片没有前缀，以前只能靠猜'
        assert by_id[4]['sender_side'] == SIDE_UNKNOWN, 'Name2Id 缺失且无证据 ⇒ 不许猜'
        assert by_id[5]['sender_side'] == SIDE_SYSTEM
        assert by_id[5]['sender_name'] == '系统消息'
        assert by_id[6]['sender_side'] == SIDE_ME
        assert by_id[6]['sender_evidence'] == SOURCE_ORIGIN

    def test_is_sender_stays_backward_compatible(self, tmp_path):
        dec, _t = _make_shard(tmp_path)
        res = M.query_messages(dec, CHAT, wxid=OWN_DIR, per_page=50)
        for m in res['messages']:
            assert m['is_sender'] == (m['sender_side'] == SIDE_ME)

    def test_sender_filter_matches_media_rows_without_prefix(self, tmp_path):
        """筛某人时必须**包含没有正文前缀的媒体消息**（旧实现永远筛不出来）。"""
        dec, _t = _make_shard(tmp_path)
        res = M.query_messages(dec, CHAT, wxid=OWN_DIR, sender=CHAT, per_page=50)
        ids = {m['id'] for m in res['messages']}
        assert ids == {1, 3}, '对方的两条（文本 + 图片）都必须命中，实际 %s' % ids

    def test_sender_filter_self_uses_name2id_not_just_origin(self, tmp_path):
        dec, _t = _make_shard(tmp_path)
        res = M.query_messages(dec, CHAT, wxid=OWN_DIR, sender='__self__', per_page=50)
        ids = {m['id'] for m in res['messages']}
        assert ids == {2, 6}, 'Name2Id 判为我(2) 与 origin=1(6) 都要在，实际 %s' % ids

    def test_sender_filter_unknown_bucket(self, tmp_path):
        """`__unknown__` = Name2Id 里没有该 rsid（且不是系统/不是因为 origin=1 才判我）。"""
        dec, _t = _make_shard(tmp_path)
        res = M.query_messages(dec, CHAT, wxid=OWN_DIR, sender='__unknown__', per_page=50)
        ids = {m['id'] for m in res['messages']}
        assert ids == {4}, '只有 4 既无 Name2Id 也无其它信号，实际 %s' % ids


class TestChatStatsUsesTheModel:
    def test_distribution_keys_and_counts(self, tmp_path):
        dec, _t = _make_shard(tmp_path)
        stats = M.get_chat_stats(dec, CHAT, wxid=OWN_DIR)
        dist = stats['sender_distribution']
        assert stats['total_messages'] == 6
        assert dist['__self__']['count'] == 2            # 2(Name2Id) + 6(origin)
        assert dist['__sys__']['count'] == 1             # 5
        assert dist[CHAT]['count'] == 2                  # 1 + 3
        assert dist['__unknown__']['count'] == 1         # 4
        assert dist['__self__']['name'] == '我'
        assert dist['__sys__']['name'] == '系统消息'

    def test_counts_are_not_lumped_into_the_other_party(self, tmp_path):
        """历史缺陷：单聊里"几乎全部归类为对方"。现在必须按权威判据分开。"""
        dec, _t = _make_shard(tmp_path)
        stats = M.get_chat_stats(dec, CHAT, wxid=OWN_DIR)
        assert stats['sender_distribution'][CHAT]['count'] < stats['total_messages']
