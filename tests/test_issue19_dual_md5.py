# -*- coding: utf-8 -*-
"""issue #19 回归：hardlink DB 的**两套 md5**（CDN md5 与文件 md5）都必须能被解析。

问题（issue #19 作者定位准确）
------------------------------
`<x>_hardlink_info_v4` 里

* `md5` 列    = **CDN 资源 md5**
* `file_name` = `<文件 md5>_W.dat` / `<文件 md5>.dat`（本地文件名）

而消息 XML / `packed_info_data` 里**两种形态都会出现**。旧代码在**解析与出图这两条主路径**上
只查 `WHERE md5=?`：

* 控制方本机实测：60/60 条图片消息请求的 md5 **只命中 `file_name`、不命中 `md5` 列**；
* 于是回落到 `MessageResourceInfo` 的 local_id 兜底，而那条兜底在本机 60 条里
  **9 条（15%）取到的是别的图** ⇒ 用户看到"部分图片显示成另一张图 / 尺寸明显不对"。

修法：解析链与出图链**都改成双键查询**（先 `md5` 列，再 `file_name` 前缀），
并且给 local_id 兜底加**交叉校验**：证明不了是同一张图就**不用它**
（宁可显示"媒体文件未找到"，也不显示错图）。

本文件全部合成，不读任何真实数据。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import media  # noqa: E402
from engine.services.message import media_resolve as MR  # noqa: E402

CDN_MD5 = 'a' * 32            # md5 列（CDN 资源 md5）
FILE_MD5 = 'b' * 32           # 文件 md5（= file_name 前缀）
OTHER_MD5 = 'c' * 32          # 另一张图
CHAT = 'wxid_friend_1a2b'
ACCOUNT = 'wxid_me12cd34ef56_9f2c'
D1 = 'dirhash1'
D2 = '2026-05'
IMG_NAME = FILE_MD5 + '_W.dat'
THUMB_NAME = FILE_MD5 + '_h.dat'


def wrap(field: int, payload: bytes) -> bytes:
    """把一个 bytes 字段包成 protobuf 的 LEN 记录（长度 <128，字段 <16）。"""
    assert field < 16 and len(payload) < 128
    return bytes([(field << 3) | 2, len(payload)]) + payload


def _setup(tmp_path, *, resource_name=None, make_file=True):
    """造 decrypted/：hardlink.db（含双键行）+ message_resource.db + 实机存储根。

    ``resource_name``：写入 `MessageResourceInfo` 的候选文件名（模拟 local_id 兜底给出的东西）。
    """
    dec = tmp_path / 'decrypted'
    (dec / 'hardlink').mkdir(parents=True, exist_ok=True)
    (dec / 'message').mkdir(parents=True, exist_ok=True)
    root = tmp_path / 'xwechat_files'
    img_dir = root / ACCOUNT / 'msg' / 'attach' / D1 / D2 / 'Img'
    img_dir.mkdir(parents=True, exist_ok=True)
    if make_file:
        (img_dir / IMG_NAME).write_bytes(b'\xff\xd8\xff\xe0' + b'x' * 100)

    # hardlink.db
    hl = sqlite3.connect(str(dec / 'hardlink' / 'hardlink.db'))
    hl.execute('CREATE TABLE db_info (Key TEXT, ValueStdStr TEXT)')
    hl.execute("INSERT INTO db_info VALUES ('uuid', ?)", ('1_' + 'x' * 20 + '_' + str(root),))
    hl.execute('CREATE TABLE dir2id (name TEXT, username TEXT)')
    hl.execute('INSERT INTO dir2id VALUES (?, ?)', (D1, D1))
    hl.execute('INSERT INTO dir2id VALUES (?, ?)', (D2, D2))
    for t in ('image_hardlink_info_v4', 'video_hardlink_info_v4', 'file_hardlink_info_v4'):
        hl.execute('CREATE TABLE [%s] (md5 TEXT, file_name TEXT, file_size INTEGER, '
                   'dir1 INTEGER, dir2 INTEGER)' % t)
        # 关键：**同一行**的两个 md5 不同（md5 列 = CDN，file_name 前缀 = 文件 md5）
        hl.execute('INSERT INTO [%s] VALUES (?,?,?,1,2)' % t, (CDN_MD5, IMG_NAME, 1234))
        hl.execute('INSERT INTO [%s] VALUES (?,?,?,1,2)' % t, (CDN_MD5, THUMB_NAME, 99))
        # 第二张图（供"local_id 兜底取到别的图"这一场景使用：候选行必须真实存在）
        hl.execute('INSERT INTO [%s] VALUES (?,?,?,1,2)' % t,
                   (CDN_MD5, OTHER_MD5 + '_W.dat', 777))
    hl.commit()
    hl.close()

    # message_resource.db（local_id 兜底的数据源）
    res = sqlite3.connect(str(dec / 'message' / 'message_resource.db'))
    res.execute('CREATE TABLE ChatName2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    res.execute('INSERT INTO ChatName2Id (rowid, user_name) VALUES (7, ?)', (CHAT,))
    res.execute('CREATE TABLE MessageResourceInfo (chat_id INTEGER, message_local_id INTEGER, '
                'message_local_type INTEGER, packed_info BLOB)')
    if resource_name:
        packed = wrap(2, wrap(1, resource_name.encode()))
        res.execute('INSERT INTO MessageResourceInfo VALUES (7, 42, 3, ?)', (packed,))
    res.commit()
    res.close()
    return str(dec)


# ---------------------------------------------------------------------------
# A. 双键查询 helper
# ---------------------------------------------------------------------------

class TestHardlinkDualKeyLookup:
    def _conn(self, tmp_path):
        dec = _setup(tmp_path)
        return sqlite3.connect(os.path.join(dec, 'hardlink', 'hardlink.db'))

    def test_hits_by_file_md5_when_cdn_column_differs(self, tmp_path):
        conn = self._conn(tmp_path)
        rows = media._hardlink_rows_by_keys(conn, 'image_hardlink_info_v4', FILE_MD5)
        conn.close()
        assert rows, '按文件 md5（file_name 前缀）必须能查到 —— 这是 issue #19 的核心'
        assert rows[0][0] == IMG_NAME, '应该优先给原图（而不是 _h 缩略图）'

    def test_hits_by_cdn_md5_as_before(self, tmp_path):
        conn = self._conn(tmp_path)
        rows = media._hardlink_rows_by_keys(conn, 'image_hardlink_info_v4', CDN_MD5)
        conn.close()
        assert rows and rows[0][0] == IMG_NAME

    def test_unknown_md5_returns_empty(self, tmp_path):
        conn = self._conn(tmp_path)
        assert media._hardlink_rows_by_keys(conn, 'image_hardlink_info_v4', 'e' * 32) == []
        conn.close()


class TestResolveFromHardlinkDb:
    @pytest.mark.parametrize('media_type,table,expect_prefix', [
        (3, 'image', 'msg/attach/'),
        (43, 'video', 'msg/video/'),
        (6, 'file', 'msg/file/'),
    ])
    def test_resolves_by_file_md5(self, tmp_path, media_type, table, expect_prefix):
        dec = _setup(tmp_path)
        rel = media._resolve_from_hardlink_db(dec, FILE_MD5, media_type)
        assert rel, 'media_type=%s 按文件 md5 也必须能定位（issue #19）' % media_type
        assert rel.startswith(expect_prefix) and rel.endswith(IMG_NAME)

    def test_still_resolves_by_cdn_md5(self, tmp_path):
        dec = _setup(tmp_path)
        assert media._resolve_from_hardlink_db(dec, CDN_MD5, 3).endswith(IMG_NAME)

    def test_serving_path_finds_the_real_file(self, tmp_path):
        """端到端（出图链）：请求 md5 = 文件 md5 时，_resolve_hardlink_path 必须给**对的**那个文件。"""
        dec = _setup(tmp_path)
        got = media._resolve_hardlink_path(
            dec, {'md5': FILE_MD5, 'local_path': '', 'media_type': 3,
                  'file_name': '', 'local_id': 0}, ACCOUNT)
        assert got and os.path.isfile(got), '应当解析出磁盘上的真实文件'
        assert os.path.basename(got) == IMG_NAME, '不许退化成 _h 缩略图（“尺寸明显不对”）'


# ---------------------------------------------------------------------------
# B. 解析链（XML 路径 / packed_info 路径）
# ---------------------------------------------------------------------------

class TestXmlPathUsesBothKeys:
    def test_xml_md5_in_file_form_resolves(self, tmp_path):
        dec = _setup(tmp_path)
        xml = '<msg><img md5="%s" aeskey="x"/></msg>' % FILE_MD5
        r = MR._resolve_media_from_xml(xml, dec, 3)
        assert r and r.get('local_path', '').endswith(IMG_NAME), \
            'XML 里带文件 md5 时也必须能定位（旧代码返回 None）'

    def test_xml_md5_in_cdn_form_still_resolves(self, tmp_path):
        dec = _setup(tmp_path)
        xml = '<msg><img md5="%s"/></msg>' % CDN_MD5
        r = MR._resolve_media_from_xml(xml, dec, 3)
        assert r and r.get('local_path', '').endswith(IMG_NAME)


class TestProtoPathUsesBothKeys:
    def _packed(self, md5):
        return wrap(3, wrap(4, md5.encode()))

    def test_packed_info_md5_in_file_form_resolves(self, tmp_path):
        dec = _setup(tmp_path)
        r = MR._resolve_media_from_proto(dec, self._packed(FILE_MD5), 3, chat_id=CHAT, local_id=42)
        assert r and r.get('local_path', '').endswith(IMG_NAME)

    def test_packed_info_md5_in_cdn_form_still_resolves(self, tmp_path):
        dec = _setup(tmp_path)
        r = MR._resolve_media_from_proto(dec, self._packed(CDN_MD5), 3, chat_id=CHAT, local_id=42)
        assert r and r.get('local_path', '').endswith(IMG_NAME)


# ---------------------------------------------------------------------------
# C. local_id 兜底的交叉校验（防"显示成另一张图"）
# ---------------------------------------------------------------------------

class TestResourceFallbackIsVerified:
    def _packed(self, md5):
        return wrap(3, wrap(4, md5.encode()))

    def test_mismatching_resource_candidate_is_rejected(self, tmp_path, capsys):
        """local_id 兜底给出的是**另一张图**的文件名 ⇒ 不许采用（旧行为会直接显示错图）。

        构造：请求一个**格式合法**（32 位十六进制）但两列都查不到的 md5，
        而 `MessageResourceInfo` 给出的候选是另一张图的文件 ⇒ 必须拒绝 + 留日志。
        """
        dec = _setup(tmp_path, resource_name=OTHER_MD5 + '_W.dat')
        r = MR._resolve_media_from_proto(dec, self._packed('d' * 32), 3,
                                        chat_id=CHAT, local_id=42)
        out = capsys.readouterr().out
        assert not (r or {}).get('local_path'), '与请求 md5 不一致的兜底候选必须被拒绝'
        assert '拒绝' in out and 'issue #19' in out, '拒绝必须留下可诊断的日志'

    def test_no_resource_record_returns_no_local_path(self, tmp_path):
        """兜底也没有记录时：只回 md5 信息，不带 local_path（前端显示"媒体文件未找到"）。"""
        dec = _setup(tmp_path, resource_name=None)
        r = MR._resolve_media_from_proto(dec, self._packed('d' * 32), 3,
                                        chat_id=CHAT, local_id=42)
        assert r and not r.get('local_path')
