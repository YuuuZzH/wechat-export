"""`contact.extra_buffer` 解析 —— 手机号 / 性别 / 签名 / 地区 / 标签的真实来源。

为什么需要这个模块
------------------
`contact` 表里**没有** phone / sex / country / province / city / signature 这些列。
`address_book._KNOWN_EXTRA_COLS` 虽然声明了它们，但一直查不到，所以这些字段
在界面上和导出里长期是空的。真实位置是 `contact.extra_buffer` 这个 protobuf blob：

    field 2       性别      0=未知 1=男 2=女（群聊恒为 0）
    field 4       个性签名  自由文本；少数人直接把手机号填在这里
    field 5/6/7   国家/省/市
    field 14.2.1  手机号
    field 30      标签 id 串，逗号分隔，如 '4,5'

为什么不用 blackboxprotobuf
---------------------------
它是一个"猜类型"的解码器，对短 ASCII 文本会猜错：11 位手机号
`'15555555581'` 的字节恰好构成合法 protobuf，于是被解成嵌套 message
`{"6":…,"7":49}`，手机号就此丢失。

本机 24,431 条真实数据上的全量差分结果：**68/22,203 条不一致，且全部是
blackboxprotobuf 丢数据**（轻量扫描器还快 3.1 倍：0.65s vs 2.01s）。
因此这里改用确定性的 wire-format 遍历 —— 不猜类型，按 protobuf 规范走。

字段语义的依据见 `08-risks/known-issues.md` 与 `changelog.md`。
"""
import os
import re
import sqlite3

# 手机号：中国大陆 11 位手机号（13x-19x）
_PHONE_RE = re.compile(r'^1[3-9]\d{9}$')

SEX_LABELS = {0: '', 1: '男', 2: '女'}


# --------------------------------------------------------------------------
# protobuf wire format 确定性遍历
# --------------------------------------------------------------------------

def _read_varint(buf, i):
    """读一个 varint，返回 (值, 新下标)。"""
    result = 0
    shift = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError('varint too long')
    raise ValueError('truncated varint')


def iter_fields(buf):
    """按 wire format 顺序产出 (field_no, wire_type, value)。

    不做任何类型猜测：wire type 0 给 int，2 给 bytes，5/1 给定长 bytes。
    遇到非法/截断输入抛 ValueError，由调用方决定如何降级。
    """
    i = 0
    n = len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        field_no = key >> 3
        wire_type = key & 7
        if field_no == 0:
            raise ValueError('field number 0')
        if wire_type == 0:
            value, i = _read_varint(buf, i)
        elif wire_type == 2:
            length, i = _read_varint(buf, i)
            if i + length > n:
                raise ValueError('truncated length-delimited field')
            value = buf[i:i + length]
            i += length
        elif wire_type == 5:
            value = buf[i:i + 4]
            i += 4
        elif wire_type == 1:
            value = buf[i:i + 8]
            i += 8
        else:
            raise ValueError('unsupported wire type %d' % wire_type)
        yield field_no, wire_type, value


def _text(raw):
    """只接受可打印的 UTF-8 文本；二进制返回 None（避免把 blob 当文本）。"""
    if not isinstance(raw, bytes) or not raw:
        return None
    try:
        s = raw.decode('utf-8')
    except UnicodeDecodeError:
        return None
    return s if s.strip() else None


_TEXT_FIELDS = {4: 'signature', 5: 'country', 6: 'province', 7: 'city'}


def _parse_label_ids(raw):
    """'4,5' → [4, 5]。含任何非数字时整条丢弃，避免产出半个标签集。"""
    s = _text(raw)
    if not s:
        return None
    ids = []
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            return None
        ids.append(int(part))
    return ids or None


def _parse_phone_field(raw):
    """field 14 → 14.1 是号码类型，14.2.1 是号码本体。"""
    try:
        for f2, w2, v2 in iter_fields(raw):
            if f2 != 2 or w2 != 2:
                continue
            for f3, w3, v3 in iter_fields(v2):
                if f3 == 1 and w3 == 2:
                    s = _text(v3)
                    if s:
                        return s
    except ValueError:
        pass
    return None


def parse_extra_buffer(buf):
    """解析 contact.extra_buffer，返回已识别字段的 dict（未识别就不出现）。

    返回键：sex / signature / country / province / city / phone / label_ids
    手机号来自 field 4 时额外带 phone_from_signature=True。
    任何畸形输入都不抛异常 —— 坏尾截断时保留已解析到的字段。
    """
    out = {}
    if not buf or not isinstance(buf, (bytes, bytearray)):
        return out
    buf = bytes(buf)

    try:
        for field_no, wire_type, value in iter_fields(buf):
            if wire_type != 2:
                continue
            if field_no in _TEXT_FIELDS:
                s = _text(value)
                if s:
                    out[_TEXT_FIELDS[field_no]] = s
            elif field_no == 30:
                ids = _parse_label_ids(value)
                if ids:
                    out['label_ids'] = ids
            elif field_no == 14:
                phone = _parse_phone_field(value)
                if phone:
                    out['phone'] = phone
            elif field_no == 2:
                pass          # 性别是 varint，不走这条分支
    except ValueError:
        pass              # 坏尾截断：保留上面已收集到的字段

    # 性别是 varint 字段，需要单独取（上面的循环跳过了非 length-delimited）
    if 'sex' not in out:
        try:
            for field_no, wire_type, value in iter_fields(buf):
                if field_no == 2 and wire_type == 0:
                    out['sex'] = value
                    break
        except ValueError:
            pass

    # 兜底：少数联系人把手机号直接填在签名里（实测 12 例）
    if 'phone' not in out:
        sig = out.get('signature')
        if sig and _PHONE_RE.match(sig.strip()):
            out['phone'] = sig.strip()
            out['phone_from_signature'] = True
    return out


def sex_label(value):
    """0/None/未知 → 空串；1 → 男；2 → 女（容忍字符串数字）。"""
    if value is None or value == '':
        return ''
    try:
        return SEX_LABELS.get(int(value), '')
    except (TypeError, ValueError):
        return ''


# --------------------------------------------------------------------------
# contact_label：标签 id → 名称
# --------------------------------------------------------------------------

def load_labels(contact_db_path):
    """读 contact_label 表，返回 {label_id: label_name}。任何缺失都返回 {}。"""
    if not contact_db_path or not os.path.isfile(contact_db_path):
        return {}
    try:
        conn = sqlite3.connect(contact_db_path)
        try:
            rows = conn.execute(
                'SELECT label_id_, label_name_ FROM contact_label').fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out = {}
    for label_id, name in rows:
        try:
            out[int(label_id)] = (name or '').strip()
        except (TypeError, ValueError):
            continue
    return out


def resolve_labels(label_ids, labels_map):
    """[4, 5] + {4:'non_work',5:'only_work'} → ['non_work', 'only_work']。

    表里查不到的 id 从名称里跳过（原始 id 仍保留在 label_ids 中），不编造名称。
    """
    if not label_ids:
        return []
    labels_map = labels_map or {}
    out = []
    for lid in label_ids:
        try:
            name = labels_map.get(int(lid))
        except (TypeError, ValueError):
            name = None
        if name:
            out.append(name)
    return out


def load_extra_map(contact_db_path):
    """批量解析某 contact.db 的全部 extra_buffer，返回 {username: parsed}。

    空/NULL blob 不会出现在结果里（调用方据此判断"该联系人没有额外信息"）。
    """
    if not contact_db_path or not os.path.isfile(contact_db_path):
        return {}
    out = {}
    try:
        conn = sqlite3.connect(contact_db_path)
        try:
            cursor = conn.execute(
                'SELECT username, extra_buffer FROM contact '
                'WHERE extra_buffer IS NOT NULL')
            for username, blob in cursor:
                if not username:
                    continue
                if isinstance(username, bytes):
                    username = username.decode('utf-8', 'replace')
                parsed = parse_extra_buffer(blob)
                if parsed:
                    out[username] = parsed
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    return out
