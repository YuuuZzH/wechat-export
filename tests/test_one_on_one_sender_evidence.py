"""P0-3（GitHub issue #16 新评论）：单聊里"你我"归属必须有**内容级证据**。

症状（报告者原话）
------------------
> 有时候聊天窗口识别你我双方会出现错误，比如对方发送的消息识别成我发送的……
> 有可能对方给我发送了三条消息，程序解析出来却有两条是我发的，而另一条是对方发送的。

机制（控制方本机真实数据实测）
------------------------------
`_row_to_message` 的单聊兜底里有一条**无证据的默认判定**：`sender_map` 非空但这一行的
`real_sender_id` 不在表里 ⇒ 直接认定"是我发的"。语音(34)/文件·引用(49)/表情(47)
这类消息**没有 `sender:\\n` 正文前缀**，正文判定帮不上忙 ⇒ 对方的这些消息被标成"我"：

| 口径 | 结果 |
|---|---|
| 可证明"对方发的"行（payload 的 fromusername == chat_id） | 968 行里 **42 行被标成"我"**（4.3%） |
| 其中 chat_id 不带 `wxid_` 前缀（自定义微信号） | **6.1%**（`wxid_` 会话 2.8%） |
| 可证明"本人发的"行却被标成对方 | 681 行里 **49 行**（7.2%） |

修法：**内容级证据优先**，有证据以证据为准；没有证据时**逐字退回**既有启发式
（不做无法用数据验证的翻转）。
  * 正文前缀 `X:\\n` —— `X` 是本人任一形态 ⇒ 自己发的；否则 ⇒ 对方发的；
  * payload 里**本条消息自身**的 `fromusername`（引用块 `<refer>`/`<refermsg>` 里的不算）
    —— 等于 `chat_id` ⇒ 对方发的；等于本人任一形态 ⇒ 自己发的；两者同时出现 ⇒ 不判。

全部**合成**，不需要任何真实数据。
"""
import pytest

from engine.services.message import (_content_sender_evidence, _own_id_forms,
                                     _payload_fromusername_values, _row_to_message)

CHAT = 'myfriend_68f8'          # 单聊：chat_id 就是对方（这里刻意不带 wxid_ 前缀）
OWN_DIR = 'wxid_me12cd34ef56_9f2c'   # 账号目录名（app.config['WXID'] 实测就是这种）
OWNS = _own_id_forms(OWN_DIR)


def _row(content, ltype=49, origin=0, rsid=0, local_id=1, ts=1700000000):
    """按 `_row_to_message` 期望的列序造一行。"""
    return (local_id, ltype, origin, ts, 3, content, rsid, None)


def _appmsg(fromusername, extra=''):
    return ('<msg><appmsg><title>t</title></appmsg>'
            '<fromusername>%s</fromusername>%s</msg>' % (fromusername, extra))


# ---------------------------------------------------------------------------
# A. 证据函数本身
# ---------------------------------------------------------------------------

class TestOwnIdForms:
    def test_covers_dir_name_and_bare_id(self):
        assert OWN_DIR in OWNS
        assert 'wxid_me12cd34ef56' in OWNS

    def test_covers_a_custom_alias_dir(self):
        """自定义微信号：目录名 `<微信号>_68f8`，`bare_wxid` 剥不掉，候选里必须有裸形态。"""
        forms = _own_id_forms('myalias_68f8')
        assert 'myalias' in forms and 'myalias_68f8' in forms

    def test_empty_yields_empty_set(self):
        assert _own_id_forms(None) == set()
        assert _own_id_forms('') == set()


class TestPayloadFromusername:
    def test_extracts_the_messages_own_sender(self):
        assert _payload_fromusername_values(_appmsg('someone')) == {'someone'}

    def test_ignores_the_quoted_messages_sender(self):
        """引用块里的 fromusername 是**被引用那条**的发送者，不能当成本条的发送者。"""
        content = ('<msg><appmsg><title>t</title><refermsg>'
                   '<fromusername>quoted_person</fromusername>'
                   '</refermsg></appmsg><fromusername>me</fromusername></msg>')
        assert _payload_fromusername_values(content) == {'me'}

    def test_no_field_returns_empty(self):
        assert _payload_fromusername_values('hello world') == set()
        assert _payload_fromusername_values(None) == set()
        assert _payload_fromusername_values(b'\x00\x01\x02') == set()


class TestContentSenderEvidence:
    def test_prefix_of_other_person_is_other(self):
        assert _content_sender_evidence('someoneelse:\nhi', CHAT, OWNS) == 'other'

    def test_prefix_of_own_form_is_self(self):
        assert _content_sender_evidence('wxid_me12cd34ef56:\nhi', CHAT, OWNS) == 'self'

    def test_fromusername_equal_to_chat_id_is_other(self):
        assert _content_sender_evidence(_appmsg(CHAT), CHAT, OWNS) == 'other'

    def test_fromusername_equal_to_own_form_is_self(self):
        assert _content_sender_evidence(_appmsg('wxid_me12cd34ef56'), CHAT, OWNS) == 'self'

    def test_both_present_is_undecidable(self):
        content = _appmsg(CHAT, '<fromusername>wxid_me12cd34ef56</fromusername>')
        assert _content_sender_evidence(content, CHAT, OWNS) is None

    def test_no_evidence_returns_none(self):
        assert _content_sender_evidence('plain text', CHAT, OWNS) is None
        assert _content_sender_evidence('', CHAT, OWNS) is None


# ---------------------------------------------------------------------------
# B. 端到端：报告者报的那两类错
# ---------------------------------------------------------------------------

class TestTheCounterpartsMediaIsNoLongerShownAsMine:
    def test_an_unmapped_rsid_with_counterpart_evidence_is_not_mine(self):
        """RED（本机实测 39/42 行是这个形状）：rsid 不在 map 里 + payload 说是对方 ⇒ 必须不是"我"。"""
        smap = {7: '__other__'}          # map 非空，但这一行的 rsid=9 不在里面
        msg = _row_to_message(_row(_appmsg(CHAT), rsid=9), CHAT,
                              sender_map=smap, own_wxid=OWN_DIR)
        assert msg['is_sender'] is False, \
            '对方发的文件/引用消息被显示成"我发的" —— 这正是报告者报的那两条'

    def test_an_rsid_mapped_to_own_is_overridden_by_counterpart_evidence(self):
        """本机实测 3/42 行是"map 显式判给我"但也带着对方证据 ⇒ 证据优先。"""
        smap = {9: OWN_DIR}
        msg = _row_to_message(_row(_appmsg(CHAT), rsid=9), CHAT,
                              sender_map=smap, own_wxid=OWN_DIR)
        assert msg['is_sender'] is False

    def test_a_quote_of_the_counterpart_still_counts_as_mine(self):
        """反向：**我引用他的话**。顶层 fromusername 是我 ⇒ 仍应算我发的（引用块不算证据）。"""
        content = ('<msg><appmsg><title>t</title><refermsg>'
                   '<fromusername>%s</fromusername></refermsg></appmsg>'
                   '<fromusername>wxid_me12cd34ef56</fromusername></msg>' % CHAT)
        smap = {9: '__other__'}          # 光看 map 会判成对方
        msg = _row_to_message(_row(content, rsid=9), CHAT,
                              sender_map=smap, own_wxid=OWN_DIR)
        assert msg['is_sender'] is True, \
            '我引用对方消息的那条被我自己的证据救回来（本机实测这类行有 49 条曾被判成对方）'

    def test_voice_from_the_other_party_is_not_mine(self):
        content = '<msg><voicemsg voicelength="3"></voicemsg><fromusername>%s</fromusername></msg>' % CHAT
        msg = _row_to_message(_row(content, ltype=34, rsid=9), CHAT,
                              sender_map={7: '__other__'}, own_wxid=OWN_DIR)
        assert msg['is_sender'] is False, '语音消息同样没有正文前缀，以前只能靠猜'

    def test_own_media_is_still_mine(self):
        content = _appmsg('wxid_me12cd34ef56')
        msg = _row_to_message(_row(content, rsid=9), CHAT,
                              sender_map={7: '__other__'}, own_wxid=OWN_DIR)
        assert msg['is_sender'] is True


# ---------------------------------------------------------------------------
# C. 不做没有依据的翻转（钉子）
# ---------------------------------------------------------------------------

class TestNoEvidenceKeepsTheOldBehaviour:
    def test_no_evidence_and_unmapped_rsid_keeps_the_historical_default(self):
        """**机制钉子**（改动前也绿）：没有任何内容证据的行，行为**逐字不变**。

        为什么故意不动它：本机 15070 行里有 13546 行属于"无证据"（无前缀文本、
        图片、系统消息…），两种可能默认值在这批行上**分歧 756 行且无法用数据判对错** ——
        悄悄翻转只会把错误换个方向（实测反向错误率 7.2% 比正向 4.74% 还高）。
        所以：**证据能救的必须救（上面那组 RED），救不了的不许乱动。**
        """
        msg = _row_to_message(_row('plain text no prefix', ltype=1, rsid=9), CHAT,
                              sender_map={7: '__other__'}, own_wxid=OWN_DIR)
        assert msg['is_sender'] is True

    def test_no_evidence_with_no_map_keeps_the_historical_default(self):
        msg = _row_to_message(_row('plain text no prefix', ltype=1, rsid=9), CHAT,
                              sender_map=None, own_wxid=OWN_DIR)
        assert msg['is_sender'] is True

    def test_origin_source_one_stays_authoritative(self):
        """`origin_source == 1`（微信自己说"本机发的"）优先级不变。

        本机实测：被误判的 40 行里没有一行是 `origin==1`（都是 origin 2/10），
        所以保留它的最高优先级**不会丢掉任何已测到的修复**。
        """
        msg = _row_to_message(_row(_appmsg(CHAT), origin=1, rsid=9), CHAT,
                              sender_map={7: '__other__'}, own_wxid=OWN_DIR)
        assert msg['is_sender'] is True


# ---------------------------------------------------------------------------
# D. 群聊不受影响
# ---------------------------------------------------------------------------

class TestGroupChatsAreUntouched:
    def test_group_row_keeps_the_map_behaviour(self):
        """证据函数只对单聊有意义（群 chat_id 是群 id、fromusername 是成员）⇒ 群聊路径不变。"""
        row = _row(_appmsg('some_member'), ltype=49, rsid=9)
        msg = _row_to_message(row, 'room@chatroom', sender_map={9: OWN_DIR},
                              own_wxid=OWN_DIR)
        assert msg['is_sender'] is True, '群聊仍按 origin/map 判定（本次不碰群聊语义）'

    def test_group_text_with_prefix_still_resolves_the_member(self):
        content = 'member_wxid:\nactual group message'
        msg = _row_to_message(_row(content, ltype=1), 'room@chatroom',
                              own_wxid=OWN_DIR)
        assert msg['is_sender'] is False
        assert msg['sender_name'] == 'member_wxid'
        assert msg['content'] == 'actual group message'
