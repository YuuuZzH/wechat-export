"""Message query service for WeChat 4.x per-chat Msg_<hash> tables."""
import hashlib
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from engine.parsers import PARSERS
from engine.parsers import types as _  # trigger parser registration
from engine.services.emoji_map import translate_wechat_emoji
from engine.services.name_resolver import resolve_wxid, pick_display_name as _pick_display_name
from engine.services.sender_model import (ShardSenderModel as _sender_model, load_name2id,
                                          SIDE_ME, SIDE_OTHER, SIDE_SYSTEM, SIDE_UNKNOWN,
                                          SOURCE_NAME2ID)
from engine.services.message.media_resolve import (
    _resolve_media_from_proto, _lookup_resource_file_name,
    _resolve_voice_path, _scan_filesystem_for_media, _resolve_via_resource_db,
    _resolve_media_from_xml
)

# zstd 解压上下文按线程各持一个（zstandard 的上下文不是线程安全的，见 decode.py）。
from engine.services.message.decode import get_zstd_decompressor  # noqa: E402

# WeChat 4.x uses high bits of local_type for flags (e.g. type 49 = 0x500000031).
# The actual message type is in the lower 16 bits.
_LOCAL_TYPE_MASK = 0xFFFF

# WeChat 4.x zstd compression magic (NOT protobuf — zstd frame header)
_ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'


def _zstd_decompress_xml(data: bytes) -> str:
    """Decompress WeChat 4.x zstd-compressed message_content to XML text.

    WeChat 4.x stores message metadata as zstd-compressed XML (NOT protobuf).
    The magic bytes 28 B5 2F FD identify the zstd frame header.
    Strips sender prefix ("wxid_xxx:\\n" or "username:\\n") before XML content.

    Returns decompressed XML string, or None on failure.
    """
    ctx = get_zstd_decompressor()
    if ctx is None or len(data) < 4 or data[:4] != _ZSTD_MAGIC:
        return None
    try:
        raw = ctx.decompress(data, max_output_size=50 * 1024 * 1024)
    except Exception:
        return None

    # Strip sender prefix (e.g. "wxid_xxx:\\n" or "username:\\n") that WeChat
    # prepends to the XML body in zstd-compressed content for types 3/43/47 etc.
    lt_pos = raw.find(b'<')
    if lt_pos > 0:
        raw = raw[lt_pos:]

    # Try UTF-8 first, then GBK for CDATA content
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        try:
            text = raw.decode('gbk', errors='replace')
        except Exception:
            text = raw.decode('utf-8', errors='replace')

    # Handle CDATA sections that may contain GBK-encoded bytes within UTF-8 XML
    if '�' in text:
        text = _fix_cdata_encoding(raw)

    return text.strip()


def _zstd_decompress_raw(data: bytes) -> bytes:
    """Decompress zstd content WITHOUT stripping sender prefix.

    Used by _build_sender_map to extract wxid from the ``sender:\\n`` prefix
    that _zstd_decompress_xml normally strips.
    """
    ctx = get_zstd_decompressor()
    if ctx is None or len(data) < 4 or data[:4] != _ZSTD_MAGIC:
        return None
    try:
        return ctx.decompress(data, max_output_size=50 * 1024 * 1024)
    except Exception:
        return None


def _fix_cdata_encoding(raw: bytes) -> str:
    """Fix mixed-encoding XML where CDATA sections may be GBK-encoded.

    WeChat 4.x XML sometimes has UTF-8 structure with GBK-encoded CDATA content.
    This detects CDATA blocks and re-decodes them as GBK.
    """
    result = bytearray()
    i = 0
    while i < len(raw):
        # Find CDATA start
        cdata_start = raw.find(b'<![CDATA[', i)
        if cdata_start < 0:
            result.extend(raw[i:])
            break
        # Copy everything before CDATA as UTF-8
        result.extend(raw[i:cdata_start])
        # Extract CDATA content
        cdata_content_start = cdata_start + 9  # len('<![CDATA[')
        cdata_end = raw.find(b']]>', cdata_content_start)
        if cdata_end < 0:
            # Malformed CDATA — treat rest as UTF-8
            result.extend(raw[cdata_start:])
            break
        # Check if CDATA content is valid UTF-8
        cdata_bytes = raw[cdata_content_start:cdata_end]
        try:
            cdata_text = cdata_bytes.decode('utf-8')
            # Valid UTF-8 — re-encode as UTF-8
            result.extend(b'<![CDATA[')
            result.extend(cdata_text.encode('utf-8'))
            result.extend(b']]>')
        except UnicodeDecodeError:
            # Try GBK
            try:
                cdata_text = cdata_bytes.decode('gbk')
                result.extend(b'<![CDATA[')
                result.extend(cdata_text.encode('utf-8'))
                result.extend(b']]>')
            except Exception:
                # Can't decode — keep raw with replacement chars
                result.extend(b'<![CDATA[')
                result.extend(cdata_bytes.decode('utf-8', errors='replace').encode('utf-8'))
                result.extend(b']]>')
        i = cdata_end + 3  # len(']]>')
    return result.decode('utf-8', errors='replace')


def _finderr(_e):
    pass  # xml parser error callback — ignore, regex fallback handles malformed XML


import html as _html_mod
def html_unescape(s: str) -> str:
    try:
        return _html_mod.unescape(s)
    except Exception:
        return s


def _scandir_msg_dbs(search_dir: str) -> list:
    """Scan a directory for message_<n>.db files, return [(idx, full_path), ...] sorted."""
    if not os.path.isdir(search_dir):
        return []
    dbs = []
    for f in os.listdir(search_dir):
        m = re.match(r'message_(\d+)\.db$', f, re.IGNORECASE)
        if m:
            dbs.append((int(m.group(1)), os.path.join(search_dir, f)))
    dbs.sort(key=lambda x: x[0])
    return dbs


def _find_msg_dbs(decrypted_dir: str):
    """Find message_*.db files — merges results from 'message' subdir and parent dir.

    WeChat 4.x may have message_*.db shards in both the 'message' subdirectory
    and the parent decrypted_dir. Merging ensures all shards are found so date
    range queries see the full message history.
    """
    sub_dbs = _scandir_msg_dbs(os.path.join(decrypted_dir, "message"))
    parent_dbs = _scandir_msg_dbs(decrypted_dir)
    # Merge: parent_dbs entries take precedence on index collision
    merged = {}
    for idx, path in sub_dbs:
        merged[idx] = path
    for idx, path in parent_dbs:
        merged[idx] = path
    return sorted(merged.items(), key=lambda x: x[0])


def _find_chat_db(decrypted_dir: str, chat_id: str) -> tuple:
    """Find (db_path, table_name) for a given chat_id across message_*.db files.

    Returns the FIRST matching DB (used by single-DB callers like query_message_detail).
    For multi-DB aggregation use _find_all_chat_dbs().
    """
    dbs = _find_all_chat_dbs(decrypted_dir, chat_id)
    if not dbs:
        raise FileNotFoundError(f"table Msg_<hash> not found for chat {chat_id}")
    return dbs[0]


def _find_all_chat_dbs(decrypted_dir: str, chat_id: str) -> list:
    """Find ALL (db_path, table_name) tuples for a chat across message_*.db files.

    WeChat 4.x shards messages across multiple message_N.db files. A chat's
    messages may exist in several DBs with the same Msg_<hash> table name.
    This returns ALL matching DBs sorted by index for aggregation queries.
    """
    dbs = _find_msg_dbs(decrypted_dir)
    if not dbs:
        return []

    h = hashlib.md5(chat_id.encode()).hexdigest()
    tname = f"Msg_{h}"
    result = []

    for idx, db_path in dbs:
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tname,)
            ).fetchone()
            conn.close()
            if row:
                result.append((db_path, tname))
        except sqlite3.Error:
            continue

    return result


def _build_where(start_date, end_date, msg_types, sender, keyword, is_group=False,
                 own_wxid=None):
    """Build WHERE clause and params list for WeChat 4.x Msg_ table columns.

    `sender` 的取值约定（**与前端下拉的 key 一一对应**，见 `get_chat_stats`）：

    * ``'__self__'``  —— 本人（`origin_source == 1` **或** Name2Id 指到本人 id 形态）
    * ``'__sys__'``   —— 系统消息（`local_type ∈ {10000,10002}`）
    * ``'__unknown__'`` —— 该分片 `Name2Id` 里没有这个 rsid（归属未定）
    * 其它非空值 —— 当作**具体某个人的 wxid**：用同库的
      ``real_sender_id IN (SELECT rowid FROM Name2Id WHERE user_name = ?)`` 精确匹配。
      ⚠️ 这是 issue #16 之后新增的能力：旧实现用
      ``message_content LIKE 'wxid:\\n%'``，对**没有正文前缀的媒体消息**
      （图片/语音/文件）**永远筛不出来** ⇒ 用户看到的"选某人却少了一批消息"。
      每个分片都有自己的 `Name2Id`，子查询因此天然**按分片生效**，无需逐分片拼 SQL。
    """
    clauses = ['create_time > 1000000000']
    params = []

    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date
    if start_date:
        try:
            clauses.append("create_time >= ?")
            params.append(_date_to_ts(start_date))
        except ValueError:
            pass
    if end_date:
        try:
            clauses.append("create_time <= ?")
            params.append(_date_to_ts(end_date, end_of_day=True))
        except ValueError:
            pass
    if msg_types:
        try:
            types = [int(t.strip()) for t in msg_types.split(',') if t.strip()]
        except ValueError:
            types = []
        types = [t for t in types if t in MSG_TYPE_LABELS]
        if types:
            placeholders = ','.join('?' for _ in types)
            clauses.append(f"(local_type & {_LOCAL_TYPE_MASK}) IN ({placeholders})")
            params.extend(types)
    if sender:
        own_forms = sorted(_own_id_forms(own_wxid)) if own_wxid else []
        if sender == '__self__':
            if own_forms:
                marks = ','.join('?' for _ in own_forms)
                clauses.append(
                    '(origin_source = 1 OR real_sender_id IN '
                    '(SELECT rowid FROM Name2Id WHERE user_name IN (%s)))' % marks)
                params.extend(own_forms)
            else:
                clauses.append('origin_source = 1')
        elif sender == '__sys__':
            clauses.append(f'(local_type & {_LOCAL_TYPE_MASK}) IN (10000, 10002)')
        elif sender == '__unknown__':
            # 与 `sender_model.classify()` 的兜底档**同口径**：该分片 `Name2Id` 里没有
            # 这个 rsid、`origin_source != 1`、也不是系统类型（那三类模型已分别归到
            # me / system）。⚠️ 内容级证据（zstd 里的 fromusername / 前缀）在 SQL 里
            # 看不出来，所以极少数"有内容证据"的行也会落进这个桶（本机全量 28 行）
            # —— 这个桶是**诊断用**的，不参与"谁发的"判定本身。
            clauses.append(
                'real_sender_id NOT IN (SELECT rowid FROM Name2Id)'
                ' AND origin_source != 1'
                f' AND (local_type & {_LOCAL_TYPE_MASK}) NOT IN (10000,10002)')
        else:
            # 具体某个人：按 `Name2Id` 精确匹配（群聊/单聊同一条路径）
            clauses.append(
                'real_sender_id IN (SELECT rowid FROM Name2Id WHERE user_name = ?)')
            params.append(sender)
    if keyword:
        clauses.append("message_content LIKE ? ESCAPE \'\\\'")
        params.append(f'%{_escape_like(keyword)}%')

    return ' AND '.join(clauses), params


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards % and _ in user-supplied strings."""
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _date_to_ts(date_str: str, end_of_day: bool = False) -> int:
    """Convert YYYY-MM-DD to Unix timestamp."""
    fmt = '%Y-%m-%d %H:%M:%S' if end_of_day else '%Y-%m-%d'
    if end_of_day:
        date_str = f'{date_str} 23:59:59'
    return int(datetime.strptime(date_str, fmt).timestamp())


# Regex for extracting clean WeChat IDs from potentially garbage-prefixed strings
_WXID_PATTERN = re.compile(
    r'(?:wxid_[a-z0-9]{10,20}'          # wxid_xxxxxxxxxxxxx
    r'|[a-zA-Z][a-zA-Z0-9_]{3,30}'       # alphanumeric username
    r'|[0-9]{5,20}@openim'               # openim IDs
    r'|[0-9]{5,20})'                     # numeric QQ-style IDs
)


def _clean_sender_prefix(raw_prefix: str) -> str:
    """Extract the real sender wxid/username from a potentially garbage-prefixed string.

    WeChat 4.x sometimes prepends binary protobuf data to the sender prefix,
    producing strings like '(�/� �m�\twxid_xxx'.
    This extracts the last valid ID pattern from the raw text.
    """
    if not raw_prefix:
        return ''
    # Find the last valid wxid/username in the string
    matches = list(_WXID_PATTERN.finditer(raw_prefix))
    if matches:
        return matches[-1].group(0)
    return ''


def _resolve_sender_name(decrypted_dir: str, real_sender_id: int, content_sender: str,
                         sender_map: dict = None, wxid_name_cache: dict = None) -> str:
    """Resolve a sender's display name from contact.db.

    Priority:
    1. content_sender (wxid from message prefix) — most reliable for text messages
    2. sender_map[real_sender_id] → lookup wxid in contacts — for binary messages
    3. raw wxid / ID placeholder

    In WeChat 4.x, real_sender_id does NOT map to contact.db rowid.
    It is a per-chat member index. The sender_map (built from text message
    prefixes) bridges real_sender_id to the actual wxid.

    The wxid_name_cache reduces redundant contact.db lookups within a single
    query_messages() call by caching resolved wxid → display_name mappings.
    """
    if wxid_name_cache is None:
        wxid_name_cache = {}

    # Primary: resolve by content_sender wxid (authoritative for text messages)
    if content_sender:
        if content_sender in wxid_name_cache:
            return wxid_name_cache[content_sender]
        name = resolve_wxid(decrypted_dir, content_sender)
        result = name if (name and name != content_sender) else content_sender
        wxid_name_cache[content_sender] = result
        return result

    # Fallback: use sender_map to translate real_sender_id → wxid
    if real_sender_id and real_sender_id != 0:
        cache_key = f'_rsid_{real_sender_id}'
        if cache_key in wxid_name_cache:
            return wxid_name_cache[cache_key]
        wxid = (sender_map or {}).get(real_sender_id)
        if wxid:
            if wxid == '__self__':
                result = '我'
            elif wxid == '__other__':
                result = wxid  # resolved further below
            elif wxid in wxid_name_cache:
                name = wxid_name_cache[wxid]
                result = name if (name and name != wxid) else wxid
            else:
                name = resolve_wxid(decrypted_dir, wxid)
                wxid_name_cache[wxid] = name if (name and name != wxid) else wxid
                result = name if (name and name != wxid) else wxid
        else:
            result = '未知'
        wxid_name_cache[cache_key] = result
        return result

    return '未知'


# Fallback labels when XML parsing fails (WeChat 4.x uses protobuf, not XML)
MSG_TYPE_LABELS = {
    3: '[图片]', 6: '[文件]', 34: '[语音]', 42: '[名片]',
    43: '[视频]', 47: '[表情]', 48: '[位置]', 49: '[链接]',
    50: '[网络电话]', 66: '[消息]',
}

# Types where message_content is zstd-compressed XML (28 B5 2F FD magic)
_ZSTD_TYPES = {3, 6, 42, 43, 47, 48, 49, 50}

# Types that may have XML embedded in protobuf binary (legacy, pre-zstd)
_MIXED_CONTENT_TYPES = {10000, 10002}

# Types that have pure XML content (after zstd decompression or natively)
_XML_CONTENT_TYPES = {48, 49, 3, 6, 34, 42, 43, 47, 50}


def _extract_xml_bytes(content_bytes: bytes, ltype: int) -> bytes:
    """Extract XML portion from mixed protobuf+XML content.

    WeChat 4.x message_content for some types has a binary protobuf header
    followed by XML body. This finds and extracts just the XML part.
    For type 48 (location), the entire content is pure XML.
    """
    # XML markers to search for (in priority order)
    markers = {
        49: [b'<appmsg', b'<?xml'],
        10000: [b'<sysmsg', b'<?xml'],
        10002: [b'<sysmsg', b'<?xml'],
    }
    closing = {
        b'<appmsg': b'</appmsg>',
        b'<sysmsg': b'</sysmsg>',
        b'<?xml': None,  # self-closing or has child elements
        b'<msg': b'</msg>',
    }

    search_markers = markers.get(ltype, [b'<msg', b'<?xml', b'<appmsg', b'<sysmsg'])

    best_xml = None
    best_len = 0

    for marker in search_markers:
        idx = content_bytes.find(marker)
        if idx < 0:
            continue

        xml_part = content_bytes[idx:]

        # Try to find closing tag
        end_tag = closing.get(marker)
        if end_tag:
            end_idx = xml_part.find(end_tag)
            if end_idx > 0:
                xml_part = xml_part[:end_idx + len(end_tag)]

        # Validate by attempting parse
        try:
            ET.fromstring(xml_part)
            if len(xml_part) > best_len:
                best_xml = xml_part
                best_len = len(xml_part)
        except ET.ParseError:
            # Try finding the XML declaration and reparsing from there
            decl_idx = xml_part.find(b'<?xml')
            if decl_idx > 0:
                xml_part2 = xml_part[decl_idx:]
                for et in closing.values():
                    if et:
                        ei = xml_part2.find(et)
                        if ei > 0:
                            candidate = xml_part2[:ei + len(et)]
                            try:
                                ET.fromstring(candidate)
                                if len(candidate) > best_len:
                                    best_xml = candidate
                                    best_len = len(candidate)
                            except ET.ParseError:
                                pass

    return best_xml


# ---------------------------------------------------------------------------
# 内容级「发送者证据」（GitHub issue #16 新评论：单聊里对方的消息被显示成"我"）
# ---------------------------------------------------------------------------
# 症状与机制（本机真实数据实测，见会话报告）：
#   * `_row_to_message` 的单聊兜底里有一条**无证据的默认判定**：
#     `sender_map` 非空但这一行的 `real_sender_id` 不在表里 ⇒ 直接认定"是我发的"。
#   * 语音(34)/文件·引用(49)/表情(47) 这类消息**没有 `sender:\n` 正文前缀**，
#     正文判定帮不上忙，于是**对方的这些消息被标成"我"**（本机 968 条可判定行里 42 条，
#     4.3%；chat_id 不带 `wxid_` 前缀的会话里 6.1%，是 `wxid_` 会话的两倍以上）。
#   * 反过来也有错：本机 681 条可判定"确实是本人发的"行里有 49 条（7.2%）被标成对方。
# 修法：**先看内容里的硬证据**（下面两个），有证据就以证据为准；没有证据才退回既有的
# rsid/启发式那一套（**行为逐字不变**）——因为"没有证据"的那些行无法用数据判对错，
# 悄悄翻转它们只会把错误换个方向（本机实测：两种默认值在 13546 条无证据行上分歧 756 条，
# 无法评分）。**没有证据时"猜"仍然是猜，只是不再覆盖有证据的结论。**

# 引用块：`<refermsg>`/`<refer>` 里的 fromusername 是**被引用那条消息**的发送者。
# 不剥掉它，会把"我引用他的话"判成"他发的"。
_REFER_BLOCK_RE = re.compile(r'<(?:refermsg|refer)\b.*?</(?:refermsg|refer)\s*>',
                             re.IGNORECASE | re.DOTALL)
_FROMUSERNAME_RE = re.compile(
    r'fromusername\s*=\s*"([^"]*)"|<fromusername>([^<]*)</fromusername>',
    re.IGNORECASE)


def _payload_fromusername_values(content):
    """取**本条消息自身**的 `fromusername` 取值集合（引用块里的不算）。

    返回 `set`（可能是空集 = payload 里没有这个字段，例如图片/系统消息）。
    """
    if isinstance(content, bytes):
        try:
            text = content.decode('utf-8', errors='replace')
        except Exception:
            return set()
    elif isinstance(content, str):
        text = content
    else:
        return set()
    if not text or 'fromusername' not in text.lower():
        return set()
    stripped = _REFER_BLOCK_RE.sub('', text)
    values = set()
    for m in _FROMUSERNAME_RE.finditer(stripped):
        val = m.group(1) if m.group(1) is not None else m.group(2)
        val = (val or '').strip()
        if val:
            values.add(val)
    return values


def _own_id_forms(own_wxid):
    """本人 id 的**可接受形式**集合（只用于比较，多一个不存在的值无害）。

    `own_wxid` 实测是**账号目录名**（`app.config['WXID']`），而消息 payload 里的
    `fromusername` 是**裸 id**；自定义微信号的目录名还形如 `<微信号>_68f8`。
    ⇒ 用 `utils.account_id_candidates()` 展开成多种形态再比较
    （它同时覆盖 `bare_wxid` 与"去 `_<4hex>` 后缀"两种规则）。

    ⚠️ 这里**只做比较**：这些形态绝不许拿去拼路径（见 `utils` 里那段说明）。
    """
    forms = set()
    try:
        from engine.utils import account_id_candidates
        forms.update(account_id_candidates(own_wxid))
    except Exception:
        pass
    if own_wxid:
        forms.add(str(own_wxid).strip())
    forms.discard('')
    return forms


def _content_sender_evidence(content, chat_id, own_forms):
    """单聊里的**内容级发送者证据**：`'self'` / `'other'` / `None`（判不了）。

    只用两种硬证据，都来自内容本身：
      ① 正文以 `X:\\n` 开头 —— 微信给**收到的**消息加发送者前缀，自己发的不加；
         `X` 是本人的任一形态 ⇒ 自己发的（转发自己的消息等），否则 ⇒ 对方发的。
      ② payload 里**本条消息自身**的 `fromusername`：等于 `chat_id`（单聊里
         `chat_id` 就是对方）⇒ 对方发的；等于本人任一形态 ⇒ 自己发的。
         两者同时出现（引用/转发混合）⇒ **不判**，交回既有启发式。

    ⚠️ **只用于单聊**：群聊的 `chat_id` 是群 id、`fromusername` 是群成员，
    语义不同，本函数不适用（调用点已限制）。
    """
    if not content:
        return None
    if isinstance(content, str):
        pos = content.find(':\n')
        if 0 < pos <= 30:
            prefix = _clean_sender_prefix(content[:pos])
            if prefix:
                return 'self' if prefix in own_forms else 'other'
    values = _payload_fromusername_values(content)
    if not values:
        return None
    has_chat = str(chat_id) in values
    has_own = bool(values & own_forms)
    if has_chat and not has_own:
        return 'other'
    if has_own and not has_chat:
        return 'self'
    return None


def _row_to_message(row, chat_id: str, decrypted_dir: str = '', parse_xml: bool = True, sender_map: dict = None, wxid_name_cache: dict = None, own_wxid: str = None, name2id: dict = None) -> dict:
    """Convert a Msg_ table row to the API message dict.

    Column order: local_id, local_type, origin_source, create_time, status,
                  message_content, real_sender_id
    """
    local_id = row[0]
    ltype = (row[1] or 0) & _LOCAL_TYPE_MASK if isinstance(row[1], (int, float)) else (row[1] or 0)
    origin = row[2]
    create_time = row[3]
    content = row[5]  # message_content
    real_sender_id = row[6] if len(row) > 6 else 0
    packed_info = row[7] if len(row) > 7 else None

    # Save raw protobuf bytes for mixed-content types (48, 49, 10000, 10002)
    # before they're stripped. WeChat 4.x embeds XML within protobuf binary;
    # standard XML parsing fails, but regex can extract key fields.
    raw_content_bytes = content if isinstance(content, bytes) else None

    if isinstance(content, bytes):
        # Check for WeChat 4.x zstd-compressed content (28 B5 2F FD = zstd frame magic).
        # After decompression, these yield XML with full message metadata.
        if len(content) >= 4 and content[:4] == _ZSTD_MAGIC:
            zstd_xml = _zstd_decompress_xml(content)
            if zstd_xml:
                content = zstd_xml
            else:
                content = ''
        else:
            try:
                decoded = content.decode('utf-8', errors='replace')
                # If the result is mostly replacement chars, treat as binary
                if decoded and len(decoded) > 0:
                    sample = decoded[:100]
                    repl_count = sample.count('�')
                    if repl_count > len(sample) * 0.3:
                        content = ''
                    else:
                        content = decoded
                else:
                    content = decoded
            except Exception:
                content = ''

    is_sender = (origin == 1)
    is_group = chat_id.endswith('@chatroom')

    # ===== 权威判定（Name2Id）=================================================
    # `name2id`（分片级 Name2Id 映射）由调用方传入时，**以它为准**：
    #   * 与内容级证据交叉验证 234,299 行 → 一致率 **99.956%**；
    #   * 单聊内部一致性（不依赖内容）：名字只落在 {本人, chat_id}，第三方为 0；
    #   * 同真值对照：旧规则错判 1.13% → 本方法 **0.056%**（178,759 行实测）。
    # 详见 `engine/services/sender_model.py` 的模块说明。
    _model = None
    _side = _src = _member = None
    if name2id is not None and own_wxid:
        _model = _sender_model(name2id, chat_id, own_wxid)
        _side, _src, _member = _model.classify(real_sender_id, origin, content,
                                               local_type=ltype)
        is_sender = (_side == SIDE_ME)

    # issue #16 新评论：**内容级证据优先**（没有 Name2Id 映射时的路径，详见
    # `_content_sender_evidence` 的说明）。
    # 位置刻意放在这里：
    #   * `origin == 1`（微信自己说"这条是本机发的"）**保持最高优先级**，行为不变；
    #   * 只有单聊走这条路（群聊的 chat_id/fromusername 语义不同）；
    #   * 有证据 ⇒ 以证据为准（这同时修掉两个方向的错：对方的语音/文件被标成"我"、
    #     以及本人消息被标成对方）；
    #   * **没有证据 ⇒ 逐字退回**下面那套既有启发式（不做无法验证的翻转）。
    if _model is None and not is_group and not is_sender:
        _evidence = _content_sender_evidence(content, chat_id, _own_id_forms(own_wxid))
        if _evidence is not None:
            is_sender = (_evidence == 'self')
        elif isinstance(content, str) and ':\n' in content[:100]:
            parts = content.split(':\n', 1)
            cs = _clean_sender_prefix(parts[0])
            if cs and own_wxid and cs == own_wxid:
                is_sender = True
            # else: prefix from other person, is_sender stays False
        elif real_sender_id and real_sender_id != 0 and sender_map:
            wxid_from_map = sender_map.get(int(real_sender_id))
            # _build_sender_map may use '__self__' sentinel when own_wxid is
            # None (strategies 2/3). Must check both the real wxid and the sentinel.
            if wxid_from_map and ((own_wxid and wxid_from_map == own_wxid) or wxid_from_map == '__self__'):
                is_sender = True
            elif wxid_from_map is None:
                # sender_map is non-empty but this rsid isn't in it → self-sent.
                # In 1-on-1 chats, only the other person's messages carry the
                # "sender_wxid:\n" prefix, so sender_map only maps their rsid.
                # Any rsid NOT in sender_map must be our own.
                # ⚠️ **这是无证据的默认判定**（issue #16 新评论的根因）：
                # 本机实测它会把"对方的语音/文件/引用"判成"我"（968 条可判定行里 42 条），
                # 也会把"我发的"判成对方（49/681）。上面那条内容级证据会**先**拦下
                # 绝大多数此类行；真正走到这里的是**没有任何内容证据**的行
                # （图片 / 无前缀文本 / 系统消息），两种默认值在这批行上无法用数据判对错
                # ⇒ 这里**保持历史行为**，不悄悄翻转。
                is_sender = True
            # else: wxid_from_map exists but != own_wxid and != '__self__' → from other person
        else:
            # sender_map is empty, use per-message heuristics for 1-on-1:
            # - Content with "wxid_xxx:\\n" or "gh_xxx:\\n" prefix → other party
            #   (self-sent messages don't carry a sender prefix).
            # - Plain text without prefix → self-sent.
            # - Otherwise keep origin_source behavior.
            if isinstance(content, str) and ':\n' in content[:80]:
                prefix = content.split(':\n', 1)[0]
                if prefix.startswith('wxid_') or prefix.startswith('gh_'):
                    pass  # is_sender stays False (other party)
            elif ltype == 1 and isinstance(content, str) and content.strip():
                is_sender = True
    elif is_group and not is_sender and real_sender_id and real_sender_id != 0 and sender_map:
        wxid_from_map = sender_map.get(int(real_sender_id))
        # sender_map now includes the self rsid (mapped to own_wxid or '__self__')
        if wxid_from_map and ((own_wxid and wxid_from_map == own_wxid) or wxid_from_map == '__self__'):
            is_sender = True

    # Extract sender prefix for group messages: "sender:\nactual_content"
    if is_sender:
        sender_name = '我'
    elif _model is not None and _side == SIDE_SYSTEM:
        # 系统提示（撤回/入群/拍一拍…）——不属于任何个人
        sender_name = '系统消息'
    elif is_group:
        sender_name = chat_id
    else:
        sender_name = resolve_wxid(decrypted_dir, chat_id)
    display_content = content or ''
    content_sender = ''
    if is_group and not is_sender and isinstance(content, str) and ':\n' in content[:100]:
        parts = content.split(':\n', 1)
        content_sender = _clean_sender_prefix(parts[0])
        display_content = parts[1] if len(parts) > 1 else content

    # 展示用 content：群聊收到的消息在库里带 "sender_wxid:\n" 前缀，而这个发送者已经
    # 由 sender_name / sender_wxid 单独给出，正文里再留一份 wxid 属于重复且泄漏身份；
    # 更关键的是 Web UI 用 `white-space: pre-wrap` 渲染正文后，那个前缀会独占一行。
    # 因此 `content` 统一返回剥离后的展示文本，原始字符串保留在 `content_raw`。
    content_raw = content
    content = display_content

    # Resolve sender name for group chats
    sender_wxid = None
    if is_group and not is_sender:
        if ltype in (10000, 10002) or (_model is not None and _side == SIDE_SYSTEM):
            sender_name = '系统消息'      # System notifications — not attributed to a person
        elif _model is not None and _member:
            # Name2Id 直接给出发言人 wxid（权威）⇒ 用它解析显示名/头像
            sender_wxid = _member
            sender_name = (_model.member_label(_member, decrypted_dir, wxid_name_cache)
                           or _member)
        else:
            sender_name = _resolve_sender_name(decrypted_dir, real_sender_id, content_sender, sender_map, wxid_name_cache)
            # Derive sender_wxid for avatar URL
            if content_sender:
                sender_wxid = content_sender
            elif real_sender_id and real_sender_id != 0 and sender_map:
                sender_wxid = sender_map.get(real_sender_id)

    # Parse XML for rich media types (after stripping sender prefix)
    xml_parsed = {}
    xml_source = display_content if (is_group and not is_sender) else (content or '')

    if parse_xml and ltype != 1:
        parser = PARSERS.get(ltype)
        xml_raw = None

        if parser:
            raw_bytes = xml_source.encode('utf-8', errors='replace') if isinstance(xml_source, str) else xml_source

            if ltype in _MIXED_CONTENT_TYPES:
                # Try to extract XML from binary protobuf+XML mixed content.
                # Use raw_content_bytes (pre-strip) for WeChat 4.x protobuf content.
                source = raw_content_bytes if (raw_content_bytes and not xml_source) else raw_bytes
                extracted = _extract_xml_bytes(source, ltype)
                if extracted:
                    xml_raw = extracted
                elif raw_content_bytes:
                    # XML extraction failed — pass raw protobuf bytes to parser
                    # so it can use regex fallback to extract key fields
                    xml_raw = raw_content_bytes
            elif ltype in _XML_CONTENT_TYPES:
                xml_raw = raw_content_bytes if (raw_content_bytes and not xml_source) else raw_bytes
            else:
                # For other types, try parsing only if content looks like XML
                s = xml_source.lstrip() if isinstance(xml_source, str) else ''
                if s and (s.startswith('<') or '<msg' in s):
                    xml_raw = raw_bytes
                elif raw_content_bytes and not xml_source:
                    # WeChat 4.x protobuf content for non-mixed types (e.g. 50)
                    # — pass raw bytes to parser for regex fallback
                    xml_raw = raw_content_bytes

            if xml_raw:
                try:
                    xml_str = xml_raw.decode('utf-8', errors='replace') if isinstance(xml_raw, bytes) else xml_raw
                    xml_bytes = xml_str.encode('utf-8', errors='replace') if isinstance(xml_str, str) else xml_str
                    parsed = parser(xml_bytes)
                    xml_parsed = parsed
                except Exception:
                    pass

    # Resolve media info from protobuf packed_info_data (must precede display fallback)
    media_info = _resolve_media_from_proto(decrypted_dir, packed_info, ltype,
                                            local_id=local_id, create_time=create_time,
                                            chat_id=chat_id)

    # 语音时长以 XML 的 <voicemsg voicelength>（毫秒）为准：packed_info 的字段 1
    # 并不是真实时长（实测多条长度完全不同的语音取到的都是同一个值），曾经导致
    # 界面上所有语音都显示同一个时长（如 48″），让人误以为长语音的转写被截断。
    if ltype == 34 and isinstance(xml_parsed, dict) and xml_parsed.get('duration'):
        if media_info is None:
            media_info = {'media_type': 34}
        media_info['duration'] = xml_parsed['duration']

    # Fallback for type 49: extract md5 from XML content when protobuf lacks it.
    # Type 49 appmsg file attachments store md5 in <md5> inside <appattach>,
    # not in packed_info_data protobuf.
    if ltype == 49 and (not media_info or not media_info.get('local_path')):
        xml_fallback = _resolve_media_from_xml(xml_source, decrypted_dir, ltype)
        if not xml_fallback or not xml_fallback.get('local_path'):
            # Also try raw protobuf bytes which may embed XML text
            if isinstance(raw_content_bytes, bytes):
                raw_str = raw_content_bytes.decode('utf-8', errors='replace')
                xml_fallback = _resolve_media_from_xml(raw_str, decrypted_dir, ltype)
        if xml_fallback and xml_fallback.get('local_path'):
            media_info = xml_fallback

    # Apply emoji translation to text message content field
    if ltype == 1 and isinstance(content, str) and content:
        content = translate_wechat_emoji(content)
        if isinstance(content_raw, str) and content_raw:
            content_raw = translate_wechat_emoji(content_raw)

    # Translate emoji in xml_parsed text fields
    if xml_parsed and isinstance(xml_parsed, dict):
        for key in ('text', 'title', 'des', 'poiname', 'label'):
            val = xml_parsed.get(key)
            if isinstance(val, str) and val:
                xml_parsed[key] = translate_wechat_emoji(val)

    return {
        'id': local_id,
        'msg_type': ltype,
        'is_sender': is_sender,
        'sender_name': sender_name,
        'sender_wxid': sender_wxid,
        # 归属的可解释面（`sender_model`）：`me` / `other` / `system` / `unknown`
        # 与判定依据（`name2id` / `fromusername` / `prefix` / `origin` / `system` / `none`）。
        # 旧调用方只看 `is_sender` 也完全兼容（`is_sender == (sender_side == 'me')`）。
        'sender_side': _side if _model is not None else ('me' if is_sender else 'other'),
        'sender_evidence': _src if _model is not None else None,
        'content': content,
        'content_raw': content_raw,
        'create_time': create_time,
        'xml_parsed': xml_parsed,
        'real_sender_id': real_sender_id,
        'media_info': media_info,
    }


def _build_sender_map(conn, table_name: str, own_wxid: str = None, chat_id: str = None) -> dict:
    """Build a real_sender_id → wxid map for sender resolution.

    WeChat 4.x real_sender_id is a per-chat member index, NOT a contact.db rowid.

    Strategy (tried in order):
    1. Scan ALL message types for ``sender_wxid:\\n`` prefix (works for group
       chats where text and non-text messages carry the prefix).
    2. [1-on-1 only] Scan for ``chat_id:\\n`` prefix to identify the other
       person's rsid. Since chat_id IS the other person's wxid in 1-on-1 chats,
       this is more reliable than counting origin=1 messages.
    3. If no prefix found, fall back to origin_source=1 messages: the rsid with
       the *most* origin=1 messages is self; all other rsids are added to
       sender_map as non-self.
    4. If still no mapping and own_wxid is provided, scan non-text zstd XML
       for fromusername matching own_wxid to identify the self rsid.
       This handles 1-on-1 chats where zstd content has no :\\n prefix.
    """
    sender_map = {}
    try:
        # Strategy 1: find sender prefix in raw/decompressed content
        rows = conn.execute(
            f"""SELECT real_sender_id, message_content FROM [{table_name}]
                WHERE real_sender_id IS NOT NULL AND real_sender_id != 0
                  AND message_content IS NOT NULL
                LIMIT 2000"""
        ).fetchall()
        seen = set()
        for rsid, content in rows:
            rsid_int = int(rsid) if rsid else 0
            if rsid_int in seen or not rsid_int:
                continue
            text = None
            if isinstance(content, str):
                text = content
            elif isinstance(content, bytes) and len(content) >= 4 and content[:4] == _ZSTD_MAGIC:
                raw = _zstd_decompress_raw(content)
                if raw:
                    try:
                        text = raw.decode('utf-8')
                    except UnicodeDecodeError:
                        try:
                            text = raw.decode('gbk', errors='replace')
                        except Exception:
                            text = None
            if not text:
                continue
            pos = text.find(':\n')
            # A sender prefix is always at the very start of content
            # (e.g. "wxid_abc123:\\n" = ~21 chars). Allow up to 30 for
            # edge cases; beyond that the ":\n" is mid-content text.
            if pos <= 0 or pos > 30:
                continue
            wxid = _clean_sender_prefix(text[:pos])
            if wxid:
                sender_map[rsid_int] = wxid
                seen.add(rsid_int)

        # Strategy 2 (1-on-1 only): identify self vs other via wxid/gh_ prefix.
        # In 1-on-1 chats, received messages carry a "sender_wxid:\\n" prefix
        # while self-sent messages do NOT. The sender wxid may differ from
        # chat_id (e.g. chat_id="lucifer_sk" but prefix "wxid_abc123:\\n").
        # Any rsid with wxid/gh_ prefixed messages is the other party.
        #
        # Always runs for 1-on-1 chats, not just when sender_map is empty,
        # because strategy 1 may produce incomplete or wrong mappings (e.g.
        # self-sent forwarded messages carrying a foreign wxid prefix, or
        # unparseable prefixes that leave a rsid unmapped).
        is_one_on_one = bool(chat_id) and not chat_id.endswith('@chatroom')
        if is_one_on_one:
            pfx_rows = conn.execute(
                f"""SELECT DISTINCT real_sender_id FROM [{table_name}]
                    WHERE real_sender_id IS NOT NULL AND real_sender_id != 0"""
            ).fetchall()
            all_rsids = [int(r[0]) for r in pfx_rows if r[0]]
            if len(all_rsids) >= 1:
                other_rsid = None
                for (rsid,) in pfx_rows:
                    rsid_int = int(rsid)
                    rows = conn.execute(
                        f"""SELECT message_content FROM [{table_name}]
                            WHERE real_sender_id=? AND message_content IS NOT NULL
                            LIMIT 100""",
                        (rsid,)
                    ).fetchall()
                    for (mc,) in rows:
                        text = None
                        if isinstance(mc, str):
                            text = mc
                        elif isinstance(mc, bytes) and len(mc) >= 4 and mc[:4] == _ZSTD_MAGIC:
                            raw = _zstd_decompress_raw(mc)
                            if raw:
                                try:
                                    text = raw.decode('utf-8', errors='replace')
                                except Exception:
                                    pass
                        elif isinstance(mc, bytes):
                            try:
                                text = mc.decode('utf-8', errors='replace')
                            except Exception:
                                pass
                        if not text:
                            continue
                        pos = text.find(':\n')
                        if 0 < pos <= 80:
                            prefix = text[:pos]
                            if prefix.startswith('wxid_') or prefix.startswith('gh_'):
                                other_rsid = rsid_int
                                break
                    if other_rsid:
                        break
                if other_rsid:
                    for rsid_int in all_rsids:
                        if rsid_int != other_rsid:
                            sender_map[rsid_int] = own_wxid if own_wxid else '__self__'
                    sender_map[other_rsid] = '__other__'

        # Strategy 3: if no prefix-based mapping, use origin_source=1 to
        # identify self. In 1-on-1 chats, origin=1 reliably marks own messages
        # even when zstd content lacks the :\\n prefix.
        # IMPORTANT: only the rsid with the MOST origin=1 messages is "self".
        # Other participants may also have occasional origin=1 messages
        # (e.g. system-inserted forward confirmations).
        if not sender_map:
            origin_rows = conn.execute(
                f"""SELECT real_sender_id, COUNT(*) as cnt
                    FROM [{table_name}]
                    WHERE origin_source = 1 AND real_sender_id != 0
                    GROUP BY real_sender_id
                    ORDER BY cnt DESC"""
            ).fetchall()
            if origin_rows:
                # Only the rsid with the most origin=1 messages is self
                self_rsid = int(origin_rows[0][0])
                all_rsids = conn.execute(
                    f"""SELECT DISTINCT real_sender_id FROM [{table_name}]
                        WHERE real_sender_id != 0"""
                ).fetchall()
                for (rsid,) in all_rsids:
                    rsid_int = int(rsid)
                    if rsid_int and rsid_int != self_rsid:
                        sender_map[rsid_int] = '__other__'
                # Also add self rsid so _resolve_sender_name can find it
                if self_rsid:
                    sender_map[self_rsid] = own_wxid if own_wxid else '__self__'

        # Strategy 4: if still no mapping, scan type 47/49 zstd XML for
        # fromusername. In 1-on-1 chats, chat_id IS the other person, so
        # fromusername == chat_id identifies the other party's rsid.
        # Any other wxid_/gh_ prefixed fromusername in 1-on-1 must be the
        # user's own wxid (self-sent rich media carries own wxid).
        # For group chats, fall back to the old heuristic where any wxid_/gh_
        # prefixed fromusername indicates another participant.
        if not sender_map:
            non_text_rows = conn.execute(
                f"""SELECT real_sender_id, message_content FROM [{table_name}]
                    WHERE real_sender_id IS NOT NULL AND real_sender_id != 0
                      AND (local_type & 0xFFFF) IN (47, 49)
                      AND message_content IS NOT NULL
                    LIMIT 200"""
            ).fetchall()
            fromuser_re = re.compile(
                r'fromusername\s*=\s*"([^"]+)"|'
                r'<fromusername>([^<]+)</fromusername>'
            )
            self_rsid = None
            other_rsid = None
            for rsid, content in non_text_rows:
                text = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, bytes) and len(content) >= 4 and content[:4] == _ZSTD_MAGIC:
                    raw = _zstd_decompress_raw(content)
                    if raw:
                        try:
                            text = raw.decode('utf-8', errors='replace')
                        except Exception:
                            pass
                if not text:
                    continue
                m = fromuser_re.search(text)
                fu = m.group(1) or m.group(2) if m else None
                if fu:
                    rsid_int = int(rsid)
                    if own_wxid and fu == own_wxid:
                        self_rsid = rsid_int
                    elif is_one_on_one and fu == chat_id:
                        # chat_id IS the other person in 1-on-1 chats
                        other_rsid = rsid_int
                    elif is_one_on_one and (fu.startswith('wxid_') or fu.startswith('gh_')):
                        # In 1-on-1, any wxid_/gh_ != chat_id is own wxid
                        self_rsid = rsid_int
                    elif fu.startswith('wxid_') or fu.startswith('gh_'):
                        # Group chat: any wxid_/gh_ is another participant
                        other_rsid = rsid_int
                    if self_rsid and other_rsid:
                        break
            if self_rsid or other_rsid:
                all_rsids = conn.execute(
                    f"""SELECT DISTINCT real_sender_id FROM [{table_name}]
                        WHERE real_sender_id != 0"""
                ).fetchall()
                if self_rsid and not other_rsid:
                    # Found self but not other — mark remaining as other
                    for (rsid,) in all_rsids:
                        rsid_int = int(rsid)
                        if rsid_int and rsid_int != self_rsid:
                            sender_map[rsid_int] = '__other__'
                    sender_map[self_rsid] = own_wxid if own_wxid else '__self__'
                elif other_rsid and not self_rsid:
                    # Found other but not self — mark remaining as self
                    sender_map[other_rsid] = '__other__'
                    for (rsid,) in all_rsids:
                        rsid_int = int(rsid)
                        if rsid_int and rsid_int != other_rsid:
                            sender_map[rsid_int] = own_wxid if own_wxid else '__self__'
                else:
                    # Both found
                    for (rsid,) in all_rsids:
                        rsid_int = int(rsid)
                        if rsid_int and rsid_int != self_rsid and rsid_int != other_rsid:
                            sender_map[rsid_int] = '__other__'
                    sender_map[self_rsid] = own_wxid if own_wxid else '__self__'
                    sender_map[other_rsid] = '__other__'
    except sqlite3.Error:
        pass
    return sender_map


# `focused.message`：`found=False` 时的**人话文案**（必须明确，绝不静默）。
# 只有一种原因：行不在（筛选后的）结果集里 —— 消息可能已被删除、换了账号，
# 或当前筛选条件把它排除了。"参数非法"不走这条路，由调用方返回 400。
FOCUS_NOT_FOUND_MESSAGE = '该消息不在当前会话的查询结果中（可能已被删除、不属于当前账号，或被筛选条件排除）'


def query_messages(decrypted_dir: str, chat_id: str, wxid: str = None,
                   page: int = 1, per_page: int = 50,
                   start_date: str = None, end_date: str = None,
                   msg_types: str = None, sender: str = None,
                   keyword: str = None,
                   focus_local_id: int = None,
                   focus_create_time: int = None) -> dict:
    """Paginated message query for a specific chat.

    Pagination: most recent messages on page 1. Within a page, oldest first.

    Queries ALL message_*.db shards that contain this chat's Msg_<hash> table.
    WeChat 4.x distributes a chat's messages across multiple DB files.

    **深链定位（Task 18 / `known-issues.md` #29）**：给 `focus_local_id` +
    `focus_create_time` 时**忽略 `page`**，返回包含该消息的那一页（页内排序不变），
    并在返回值里**新增** `focused` 块。定位键必须**同时**含 `create_time`：
    `local_id` 在不同分片间会重号（`msg_meta` 主键含 `create_time`，见 ADR-0012）。
    没找到时 `focused.found=False` 且带 `reason` / `message` —— 绝不静默当成功。
    两个参数都没给时返回值**逐字段与改动前相同**（不多出 `focused` 键）。
    """
    per_page = max(1, per_page)  # guard against ZeroDivisionError
    all_dbs = _find_all_chat_dbs(decrypted_dir, chat_id)
    if not all_dbs:
        raise FileNotFoundError(f"no message_*.db found for chat {chat_id}")

    is_group = chat_id.endswith('@chatroom')
    where_clause, params = _build_where(start_date, end_date, msg_types, sender, keyword,
                                        is_group, own_wxid=wxid)

    # Build per-DB sender_maps. real_sender_id is a per-chat member index that
    # differs between WeChat DB shards, so each DB needs its own mapping.
    # 同时加载该分片的 `Name2Id`（**权威** sender 来源，见 `engine/services/sender_model.py`）。
    db_sender_maps = {}
    db_name2id = {}
    for db_path, table_name in all_dbs:
        try:
            conn = sqlite3.connect(db_path)
            db_sender_maps[db_path] = _build_sender_map(conn, table_name, own_wxid=wxid, chat_id=chat_id)
            db_name2id[db_path] = load_name2id(conn)
            conn.close()
        except sqlite3.Error:
            db_sender_maps[db_path] = {}
            db_name2id[db_path] = {}

    # Per-request cache for wxid → display_name lookups
    wxid_name_cache = {}

    # Collect and count rows from ALL DB shards, annotating with db_path
    all_rows = []
    total = 0
    for db_path, table_name in all_dbs:
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM [{table_name}] WHERE {where_clause}", params)
            total += cur.fetchone()[0]
            cur.execute(
                f"""SELECT local_id, local_type, origin_source, create_time, status,
                           message_content, real_sender_id, packed_info_data
                    FROM [{table_name}] WHERE {where_clause}""",
                params
            )
            for row in cur.fetchall():
                all_rows.append((db_path, row))
            cur.close()
            conn.close()
        except sqlite3.Error:
            continue

    if total > 0:
        total_pages = (total + per_page - 1) // per_page
        page = max(1, min(page, total_pages))
    else:
        total_pages = 0
        page = 1

    # Sort by create_time DESC across all shards, then paginate
    all_rows.sort(key=lambda r: r[1][3] or 0, reverse=True)

    # ---- 深链定位：把「目标消息在第几页」直接算出来（不需要新的查询模式）----
    # `all_rows` 已经是全量 DESC 序，所以目标的下标 → 页号只是整除：
    #     下标 i 落在第 (i // per_page + 1) 页（与下面的 offset 切分同一套语义）。
    # 只在**两个** focus 参数都给时生效（非法参数由 API 层拦成 400）。
    focus_active = focus_local_id is not None and focus_create_time is not None
    focused = None
    if focus_active:
        target_index = None
        for i, (_fdb, frow) in enumerate(all_rows):
            # 必须两列同时相等：local_id 在不同分片间会重号（行 0 = local_id，行 3 = create_time）
            if frow[0] == focus_local_id and frow[3] == focus_create_time:
                target_index = i
                break
        found = target_index is not None and total > 0
        if found:
            page = target_index // per_page + 1
            page = max(1, min(page, total_pages))
        else:
            # 没定位到也要落在**一个合理页**上，并且把原因说清楚（不得静默）
            page = 1
        focused = {
            'found': found,
            'page': page,
            'local_id': focus_local_id,
            'create_time': focus_create_time,
            'reason': None if found else 'not_in_chat',
            'message': None if found else FOCUS_NOT_FOUND_MESSAGE,
        }

    offset = (page - 1) * per_page
    page_rows = all_rows[offset:offset + per_page]

    # Reverse for oldest-first display within the page
    page_rows = list(reversed(page_rows))

    messages = []
    for db_path, row in page_rows:
        sender_map = db_sender_maps.get(db_path, {})
        msg = _row_to_message(row, chat_id, decrypted_dir, parse_xml=True,
                              sender_map=sender_map, wxid_name_cache=wxid_name_cache,
                              own_wxid=wxid, name2id=db_name2id.get(db_path))
        messages.append(msg)

    result = {
        'messages': messages,
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'total_pages': total_pages,
        },
    }
    if focused is not None:
        # 只在**给了** focus 时新增这个块（不带 focus 的调用方响应逐字段不变）
        result['focused'] = focused
    return result


def _find_all_msg_dbs(decrypted_dir: str) -> list:
    """Find all message_*.db files sorted by index."""
    return _find_msg_dbs(decrypted_dir)


def query_message_detail(decrypted_dir: str, msg_id: int, chat_id: str = '') -> dict:
    """Get full detail for a single message by its local_id.

    When chat_id is provided, queries ONLY the exact Msg_<hash> table for
    that chat to avoid cross-table local_id collisions.
    """
    dbs = _find_all_msg_dbs(decrypted_dir)
    if not dbs:
        return None

    # Phase 1: when chat_id is known, search ALL matching Msg_<hash> tables
    if chat_id:
        all_chat_dbs = _find_all_chat_dbs(decrypted_dir, chat_id)
        for chat_db_path, tname in all_chat_dbs:
            try:
                conn = sqlite3.connect(chat_db_path)
                try:
                    row = conn.execute(
                        f"""SELECT local_id, local_type, origin_source, create_time, status,
                                   message_content, real_sender_id, packed_info_data
                            FROM [{tname}] WHERE local_id=?""",
                        (msg_id,)
                    ).fetchone()
                    if row:
                        sender_map = _build_sender_map(conn, tname, chat_id=chat_id)
                        msg = _row_to_message(row, chat_id, decrypted_dir, parse_xml=True, sender_map=sender_map)
                        return msg
                finally:
                    conn.close()
            except (sqlite3.Error, OSError):
                continue
        return None  # searched all matching DBs, message not found

    # Phase 2: no chat_id provided — search all tables (slower, may collide)
    for idx, db_path in dbs:
        try:
            conn = sqlite3.connect(db_path)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (tname,) in tables:
                row = conn.execute(
                    f"""SELECT local_id, local_type, origin_source, create_time, status,
                               message_content, real_sender_id, packed_info_data
                        FROM [{tname}] WHERE local_id=?""",
                    (msg_id,)
                ).fetchone()
                if row:
                    h = tname[4:]
                    resolved_chat_id = _resolve_chat_id_from_hash(db_path, h, dbs) or tname
                    sender_map = _build_sender_map(conn, tname, chat_id=resolved_chat_id)
                    msg = _row_to_message(row, resolved_chat_id, decrypted_dir, parse_xml=True, sender_map=sender_map)
                    conn.close()
                    return msg
            conn.close()
        except sqlite3.Error:
            continue

    return None


def _resolve_chat_id_from_hash(db_path: str, h: str, dbs: list) -> str:
    """Try to resolve a Msg_ hash back to a username via Name2Id."""
    for idx, other_path in dbs:
        try:
            conn = sqlite3.connect(other_path)
            for (uname,) in conn.execute("SELECT user_name FROM Name2Id"):
                if uname and hashlib.md5(uname.encode()).hexdigest() == h:
                    conn.close()
                    return uname
            conn.close()
        except sqlite3.Error:
            pass
    return None


def get_chat_stats(decrypted_dir: str, chat_id: str, wxid: str = None,
                   include_senders: bool = True) -> dict:
    """Get statistics overview for a chat, aggregating across all message_*.db shards.

    include_senders=False 时只做 COUNT/MIN/MAX（打开会话首屏很快）；
    发送者分布再异步补全，避免全表扫描拖慢首次进会话。
    """
    all_dbs = _find_all_chat_dbs(decrypted_dir, chat_id)
    if not all_dbs:
        raise FileNotFoundError(f"no message_*.db found for chat {chat_id}")

    is_group = chat_id.endswith('@chatroom') if chat_id else False
    partner_display = ''
    if not is_group:
        partner_display = resolve_wxid(decrypted_dir, chat_id)
        if not partner_display or partner_display == chat_id:
            partner_display = chat_id

    # Aggregate basic stats across all DBs
    total = 0
    min_ts = None
    max_ts = None
    sender_dist = {}
    wxid_name_cache = {}

    for db_path, table_name in all_dbs:
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()

            cur.execute(
                f"SELECT COUNT(*), MIN(create_time), MAX(create_time) "
                f"FROM [{table_name}] WHERE create_time > 1000000000"
            )
            row = cur.fetchone()
            total += row[0] or 0
            if row[1] and (min_ts is None or row[1] < min_ts):
                min_ts = row[1]
            if row[2] and (max_ts is None or row[2] > max_ts):
                max_ts = row[2]

            if include_senders:
                # ---- 发送者归属：**唯一权威实现**（Name2Id 优先）------------------
                n2i = load_name2id(conn)
                model = _sender_model(n2i, chat_id, wxid) if wxid else None
                cur.execute(
                    f"SELECT message_content, real_sender_id, origin_source, local_type "
                    f"FROM [{table_name}] WHERE create_time > 1000000000"
                )
                for _mc, _rsid, _origin, _lt_raw in cur.fetchall():
                    _lt = ((_lt_raw or 0) & _LOCAL_TYPE_MASK
                           if isinstance(_lt_raw, (int, float)) else (_lt_raw or 0))
                    if model is not None:
                        _side, _src, _member = model.classify(
                            _rsid, _origin, _mc, local_type=_lt)
                    else:
                        _side, _member = (
                            'me' if int(_origin or 0) == 1 else 'unknown'), None
                    if _side == SIDE_ME:
                        _key, _label = '__self__', '我'
                    elif _side == SIDE_SYSTEM:
                        _key, _label = '__sys__', '系统消息'
                    elif _side == SIDE_UNKNOWN:
                        _key, _label = '__unknown__', '归属未定'
                    else:
                        _key = _member or chat_id
                        _label = (
                            model.member_label(
                                _member, decrypted_dir, wxid_name_cache)
                            if (model is not None and _member)
                            else (partner_display or chat_id)
                        ) or _key
                    _entry = sender_dist.get(_key)
                    if _entry:
                        _entry['count'] += 1
                    else:
                        sender_dist[_key] = {'name': _label, 'count': 1}

            cur.close()
            conn.close()
        except sqlite3.Error:
            continue

    date_range = {
        'start': datetime.fromtimestamp(min_ts).strftime('%Y-%m-%d') if min_ts else '',
        'end': datetime.fromtimestamp(max_ts).strftime('%Y-%m-%d') if max_ts else '',
    }

    return {
        'chat_id': chat_id,
        'total_messages': total,
        'date_range': date_range,
        'sender_distribution': sender_dist,
    }


def get_chat_dates(decrypted_dir: str, chat_id: str) -> dict:
    """Get dates that have messages and per-day counts, across all message_*.db shards."""
    all_dbs = _find_all_chat_dbs(decrypted_dir, chat_id)

    counts = {}
    for db_path, table_name in all_dbs:
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                f"""SELECT date(create_time, 'unixepoch') as d, COUNT(*) as cnt
                    FROM [{table_name}] WHERE create_time > 1000000000
                    GROUP BY d"""
            )
            for row in cur.fetchall():
                d = row[0]
                counts[d] = counts.get(d, 0) + row[1]
            cur.close()
            conn.close()
        except sqlite3.Error:
            continue

    # Sort by date descending
    sorted_counts = dict(sorted(counts.items(), key=lambda x: x[0], reverse=True))
    return {'counts': sorted_counts}
