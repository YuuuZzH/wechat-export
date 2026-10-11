"""发送者归属的**唯一权威实现**（issue #16 / `todo.txt` #3/#4/#8/#17-#20/#27/#30）。

为什么要有这个模块
------------------
微信 4.x 的 `Msg_<md5(chat_id)>` 表里**没有"谁发的"这一列**，历史上项目里有**四套并行实现**
各自推断，于是同一批数据在不同页面上结论不一致：

1. `message._row_to_message()`（聊天气泡）
2. `message.get_chat_stats()`（发送者筛选 + 我/对方计数）—— 且它拿**账号目录名**比裸 wxid，永不相等
3. `search_index`（全局搜索索引，`sender_username`）
4. `_build_sender_map()`（从正文前缀归纳 rsid→wxid）被上面几处共用

本模块把**唯一权威判据**抽出来：**分片内的 `Name2Id` 表**（`rowid` ↔ `user_name`）。

证据（2026-09-23，控制方本机真实数据，**只统计计数**）
----------------------------------------------------
* `Name2Id` 存在于**每个含消息的分片**里（2 个没有它的文件是 `message_fts.db` /
  `message_resource.db`，**不含消息行**）；`rowid` 连续 `1..N`。
* **与内容级证据交叉验证 234,299 行**（`fromusername` / 正文前缀独立推出来的"谁"）：
  **一致 234,196 行 = 99.956%**，分歧 103 行（0.044%）。
* **单聊内部一致性（不依赖内容证据）**：200 个单聊表 / 148,628 行，
  解析出的名字**只落在 {本人, chat_id} 两个参与者上，第三方 = 0 行**。
  若 `Name2Id` 映射是错的，这里必然冒出第三方名字 —— 这是最强的独立验证。
* `origin_source` 单独用会错：`origin==1` 时 Name2Id 说"本人"的占 95.7%（不是 100%）；
  `origin==10/2` 时本人/对方**都有**（10：对方 75.7% / 本人 22.7%）⇒ 它只能当兜底信号。
* `compress_content` 与 `packed_info_data` 两列**不携带发送者信息**
  （对 61,169 条无内容证据的行逐一检查，两者都没有 `fromusername`，也拿不到前缀）⇒ 不用它们。
* 残差：`Name2Id` 缺失的行占 1.86%（群聊 1.96%、单聊 0.2%），逐行有内容证据的只有极少数
  ⇒ 这批才需要退回内容证据 / `origin_source` / `unknown`。

判定优先级（每一档都写明理由，**不要随意调整**）
------------------------------------------------
1. **`Name2Id[real_sender_id]`**（分片内 rowid）：权威。命中本人 id 形态 ⇒ `me`；
   单聊里等于 `chat_id` ⇒ `other`；群聊里是成员 ⇒ `other`（并给出该成员 wxid 供显示名解析）。
2. **内容级证据**（仅当 1 拿不到）：payload 里**本条消息自身**的 `fromusername`
   （`<refer>`/`<refermsg>` 引用块里的不算）／正文 `X:\\n` 前缀。与 1 的一致率 99.95%+。
3. **`origin_source == 1`**（仅当 1、2 都拿不到）：微信自称"本机发出"，实测 95.7% 正确。
4. 否则 `unknown` —— **不猜**。调用方可以据此做中性呈现，而不是默默判成"我"。

⚠️ 与 `_build_sender_map()` 的关系：后者是**内容证据的归纳结果**，现在退居"`Name2Id`
缺失时的兜底"。它历史上的两处猜测（"rsid 不在表里 ⇒ 我"、"map 为空 + 无前缀纯文本 ⇒ 我"）
本模块一概不用 —— 本机实测这两条会把他人的语音/文件/引用判成"我"（968 行里 42 行）。
"""
import hashlib
import os
import re
import sqlite3

from engine.utils import account_id_candidates, bare_wxid

# 判据来源（可观测：写进 API 字段 / 日志，便于"为什么这么判"）
SOURCE_NAME2ID = 'name2id'
SOURCE_FROMUSERNAME = 'fromusername'
SOURCE_PREFIX = 'prefix'
SOURCE_ORIGIN = 'origin'
SOURCE_SYSTEM = 'system'
SOURCE_NONE = 'none'

# 归属
SIDE_ME = 'me'
SIDE_OTHER = 'other'
SIDE_SYSTEM = 'system'      # 系统提示（撤回/入群/拍一拍…）——**不属于任何个人**
SIDE_UNKNOWN = 'unknown'

# 系统消息类型（`local_type` 的低 16 位）
SYSTEM_TYPES = (10000, 10002)

_REFER_BLOCK_RE = re.compile(r'<(?:refermsg|refer)\b.*?</(?:refermsg|refer)\s*>',
                             re.IGNORECASE | re.DOTALL)
_REFER_BLOCK_RE_B = re.compile(rb'<(?:refermsg|refer)\b.*?</(?:refermsg|refer)\s*>',
                               re.IGNORECASE | re.DOTALL)
_FROMUSERNAME_RE = re.compile(
    r'fromusername\s*=\s*"([^"]*)"|<fromusername>([^<]*)</fromusername>', re.IGNORECASE)
_FROMUSERNAME_RE_B = re.compile(
    rb'fromusername\s*=\s*"([^"]*)"|<fromusername>([^<]*)</fromusername>', re.IGNORECASE)
_PREFIX_ID_RE = re.compile(r'[A-Za-z0-9_@\-\.]{3,40}')
_ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'


def load_name2id(conn) -> dict:
    """读一个分片的 `Name2Id`：``{rowid: user_name}``（读不到就返回空表，**不抛异常**）。

    `Name2Id` 是分片级表（`message_N.db` 各一份），所以必须**按分片**加载与使用。
    """
    out = {}
    try:
        for rid, uname in conn.execute('SELECT rowid, user_name FROM Name2Id'):
            if uname is None:
                continue
            if isinstance(uname, bytes):
                uname = uname.decode('utf-8', 'replace')
            uname = str(uname).strip()
            if uname:
                out[int(rid)] = uname
    except sqlite3.Error:
        return {}
    return out


def own_id_forms(own_wxid) -> set:
    """本人 id 的可接受形态集合（账号目录名 / 裸 wxid / 去 `_<4hex>` 后缀）。"""
    forms = set(account_id_candidates(own_wxid))
    if own_wxid:
        forms.add(str(own_wxid).strip())
        forms.add(bare_wxid(str(own_wxid)))
    forms.discard('')
    return forms


def chat_display_id(chat_id: str) -> str:
    """`Msg_<hash>` 表名里的 hash：`md5(chat_id)`（用于反查表 → 会话）。"""
    return hashlib.md5(str(chat_id).encode()).hexdigest()


def _zstd_raw(content):
    """zstd → 原始字节（**保留** `sender:\\n` 前缀；`_zstd_decompress_xml` 会截掉它）。"""
    try:
        from engine.services.message import _zstd_decompress_raw
    except Exception:
        return None
    if isinstance(content, (bytes, bytearray)) and len(content) >= 4 \
            and bytes(content[:4]) == _ZSTD_MAGIC:
        return _zstd_decompress_raw(bytes(content))
    return None


def payload_fromusernames(raw) -> set:
    """本条消息自身的 `fromusername` 取值集合（引用块内的**不计**）。"""
    if raw is None:
        return set()
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw)
        if b'fromusername' not in raw.lower():
            return set()
        stripped = _REFER_BLOCK_RE_B.sub(b'', raw).decode('utf-8', 'replace')
        out = set()
        for m in _FROMUSERNAME_RE.finditer(stripped):
            v = (m.group(1) if m.group(1) is not None else m.group(2) or '').strip()
            if v:
                out.add(v)
        return out
    if isinstance(raw, str):
        if 'fromusername' not in raw.lower():
            return set()
        stripped = _REFER_BLOCK_RE.sub('', raw)
        out = set()
        for m in _FROMUSERNAME_RE.finditer(stripped):
            v = (m.group(1) if m.group(1) is not None else m.group(2) or '').strip()
            if v:
                out.add(v)
        return out
    return set()


def content_sender_evidence(content, chat_id, own_forms, is_group=False):
    """内容级证据 → ``(side, source)``；判不了时 ``(None, SOURCE_NONE)``。

    * `fromusername == chat_id`（仅单聊；单聊里 chat_id 就是对方）⇒ other
    * `fromusername ∈ own_forms` ⇒ me
    * 正文 `X:\\n` 前缀：X ∈ own_forms ⇒ me，否则 ⇒ other（群聊里 X 是成员）
    """
    if content is None:
        return None, SOURCE_NONE
    raw = None
    if isinstance(content, (bytes, bytearray)):
        raw = _zstd_raw(content)
        if raw is None:
            raw = bytes(content)
    elif isinstance(content, str):
        raw = content
    if raw is None:
        return None, SOURCE_NONE

    vals = payload_fromusernames(raw)
    if vals:
        has_chat = (not is_group) and str(chat_id) in vals
        has_own = bool(vals & own_forms)
        if has_own and not has_chat:
            return SIDE_ME, SOURCE_FROMUSERNAME
        if has_chat and not has_own:
            return SIDE_OTHER, SOURCE_FROMUSERNAME
        if has_own and has_chat:
            return None, SOURCE_NONE
        # 群聊里 fromusername 是成员：只要不是本人 ⇒ other
        if is_group:
            return SIDE_OTHER, SOURCE_FROMUSERNAME
        return None, SOURCE_NONE

    pos = raw.find(b':\n') if isinstance(raw, (bytes, bytearray)) else raw.find(':\n')
    if 0 < pos <= 30:
        seg = raw[:pos]
        if isinstance(seg, (bytes, bytearray)):
            seg = bytes(seg).decode('utf-8', 'replace')
        toks = _PREFIX_ID_RE.findall(seg)
        if toks:
            who = toks[-1]
            return (SIDE_ME if who in own_forms else SIDE_OTHER), SOURCE_PREFIX
    return None, SOURCE_NONE


def observed_sender_id(content, own_forms):
    """从内容里取出**对方的那个 id**（payload 的 fromusername 优先，其次正文前缀）。

    只用于"已经判定是对方、但需要知道是**谁**"的场景（群聊发言人 / 导出里的显示名）。
    拿不到时返回 ``None``。
    """
    if content is None:
        return None
    raw = content
    if isinstance(content, (bytes, bytearray)):
        raw = _zstd_raw(content)
        if raw is None:
            raw = bytes(content)
    if not isinstance(raw, (bytes, bytearray, str)):
        return None
    vals = payload_fromusernames(raw)
    for v in sorted(vals):
        if v and v not in own_forms:
            return v
    pos = raw.find(b':\n') if isinstance(raw, (bytes, bytearray)) else raw.find(':\n')
    if 0 < pos <= 30:
        seg = raw[:pos]
        if isinstance(seg, (bytes, bytearray)):
            seg = bytes(seg).decode('utf-8', 'replace')
        toks = _PREFIX_ID_RE.findall(seg)
        if toks and toks[-1] not in own_forms:
            return toks[-1]
    return None


class ShardSenderModel:
    """一个分片 + 一个会话的发送者模型（不可变，可安全复用/缓存）。

    Args:
        name2id: `load_name2id(conn)` 的结果（分片级）
        chat_id: 会话 id（`Msg_<md5(chat_id)>` 里的那个 chat_id）
        own_wxid: 本人 id（调用方传**账号目录名**或裸 id 都行）
    """

    def __init__(self, name2id: dict, chat_id: str, own_wxid: str):
        self.name2id = name2id or {}
        self.chat_id = str(chat_id or '')
        self.is_group = self.chat_id.endswith('@chatroom')
        self.own_forms = own_id_forms(own_wxid)

    # -- 内部 ---------------------------------------------------------------
    def _name_of(self, rsid):
        try:
            rsid = int(rsid or 0)
        except (TypeError, ValueError):
            return None
        if rsid <= 0:
            return None
        return self.name2id.get(rsid)

    def is_own_id(self, value) -> bool:
        if not value:
            return False
        v = str(value).strip()
        if v in self.own_forms:
            return True
        return bool(set(account_id_candidates(v)) & self.own_forms)

    # -- 对外 ---------------------------------------------------------------
    def classify(self, rsid, origin_source, content=None, local_type=None):
        """返回 ``(side, source, member_wxid)``。

        * ``side`` ∈ ``'me' | 'other' | 'system' | 'unknown'``
        * ``source`` ∈ ``'name2id' | 'fromusername' | 'prefix' | 'origin' | 'system' | 'none'``
        * ``member_wxid``：判到"某个具体的人"时给出的 wxid（群聊里的发言人；
          单聊里等于 chat_id 或本人），用于解析显示名/头像；拿不到时 ``None``。
        """
        # ① Name2Id（权威）
        name = self._name_of(rsid)
        if name:
            if self.is_own_id(name):
                return SIDE_ME, SOURCE_NAME2ID, name
            if not self.is_group and name == self.chat_id:
                return SIDE_OTHER, SOURCE_NAME2ID, name
            if self.is_group:
                return SIDE_OTHER, SOURCE_NAME2ID, name
            # 单聊里出现第三方名字：Name2Id 缺失/过期才会发生（本机 0 例）⇒ 仍然信它，
            # 但把 member 交出去，便于上层展示"某人"而不是硬判成对方/我方。
            return SIDE_OTHER, SOURCE_NAME2ID, name

        # ② 内容级证据
        side, src = content_sender_evidence(content, self.chat_id, self.own_forms,
                                           is_group=self.is_group)
        if side is not None:
            if side == SIDE_OTHER:
                # 把"到底是谁"也带出去：单聊就是 chat_id；群聊就是内容里那个成员 id
                # （否则上层只能显示 `ID:<rsid>` 这种占位符）。
                member = self.chat_id if not self.is_group else \
                    observed_sender_id(content, self.own_forms)
                return side, src, member
            return side, src, None

        # ③ origin_source == 1（微信自称本机发出；实测 95.7%）
        try:
            if int(origin_source or 0) == 1:
                return SIDE_ME, SOURCE_ORIGIN, None
        except (TypeError, ValueError):
            pass

        # ④ 系统提示：不是"某个人"发的（本机实测：Name2Id 缺失的 8,997 行里 8,906 行是它）
        if local_type is not None:
            try:
                if int(local_type) & 0xFFFF in SYSTEM_TYPES:
                    return SIDE_SYSTEM, SOURCE_SYSTEM, None
            except (TypeError, ValueError):
                pass

        # ⑤ 不猜
        return SIDE_UNKNOWN, SOURCE_NONE, None

    def member_label(self, member_wxid, decrypted_dir=None, cache=None):
        """把 member_wxid 解析成显示名（拿不到就原样返回 wxid）。"""
        if not member_wxid:
            return None
        if self.is_own_id(member_wxid):
            return '我'
        if cache is not None and member_wxid in cache:
            return cache[member_wxid]
        name = member_wxid
        if decrypted_dir:
            try:
                from engine.services.name_resolver import resolve_wxid
                got = resolve_wxid(decrypted_dir, member_wxid)
                if got:
                    name = got
            except Exception:
                pass
        if cache is not None:
            cache[member_wxid] = name
        return name
