r"""全局搜索索引的生命周期：源发现、构建、增量刷新、状态。

索引落在 <解密目录>/chat_search_index.db，含：
  msg_meta     完整消息的数值元数据（100% 覆盖），供组合筛选
  message_fts  char-token 化文本，FTS5，供关键词匹配
  msg_text     原文（与 message_fts 同 rowid 的普通表），供摘要/高亮/正则
  index_meta   schema 版本 / 构建时间 / 源指纹 / 各类计数

为什么分两张表：FTS5 的 UNINDEXED 列没有 B 树索引，用它做「类型+日期」筛选
要全表扫 103 万行（实测 ~400ms）；把筛选维度放进带索引的 msg_meta 才快。

为什么 msg_text 是**普通表**而不是 message_fts 的 UNINDEXED 列（A8）：
FTS5 虚表的列无法建 B 树索引，原文只放 FTS 里的话，「无关键词的纯筛选查询」
要显示摘要就得为每页 ≤20 行各做一次 89 万行全扫（单列扫描 ~0.5–1s → 每页
10–20s，不可用），只能退化为「纯筛选结果一律无摘要」；而 spec 第 280 行只承认
「无正文消息（图片/语音/视频）无摘要」，并不承认文本消息也无摘要。
另一个 spec 未交代的后果：char-token 会小写化并去掉空白，正则在它上面确认会
**静默假阴性**（`/ERROR/`、`/[A-Z]{3}/`、`/报修\s*电话/` 在已建索引时搜不到、
在降级路径搜得到 → 同一查询因为「后来建了索引」结果变少）。原文留在 msg_text，
正则与高亮才有救。

⚠️ 迁移（A1）：`create_schema` 全是 `CREATE TABLE IF NOT EXISTS`，表名已存在时
**整条语句是 no-op** —— 旧主键（2 列）会原样留下，而构建照常写新的
`schema_version`，于是 `index_status` 报 schema_ok=True/ready=True，缺陷**自我掩盖**
（实测旧主键下 4 行被折叠成 1 行，丢 75%；真实数据语料是丢 55.4%）。
因此 `build_index` 在建 schema **之前**先验既有库的版本号**与实际主键列集合**，
不符即 DROP 重建（见 `_index_needs_rebuild` / `_migrate_schema`）。

文本语料直接来自微信自己的 message_fts_v4_*_content（普通表，已抽好的纯文本），
因此无需实现解压与 protobuf/XML 解析 —— 这是本方案复杂度的根本来源优势。
"""
import hashlib
import os
import pathlib
import re
import sqlite3
import time

from engine.services.search_query import to_char_token_text

INDEX_FILENAME = 'chat_search_index.db'
# 2：新增 msg_text（原文）与 idx_text_key —— 旧库由 A1 的迁移逻辑整库重建
SCHEMA_VERSION = 2

# 进度阶段名（与前端约定）
STAGE_META = 'meta'
STAGE_TEXT = 'text'
STAGE_OPTIMIZE = 'optimize'
STAGE_DONE = 'done'

_MSG_DB_RE = re.compile(r'^message_\d+\.db$', re.IGNORECASE)
_FTS_CONTENT_RE = re.compile(r'^message_fts_v4_\d+_content$', re.IGNORECASE)


def index_path(decrypted_dir):
    return os.path.join(decrypted_dir, INDEX_FILENAME)


def discover_message_shards(decrypted_dir):
    """找出 message_N.db（排除 message_fts.db），返回按名称排序的绝对路径。"""
    if not decrypted_dir or not os.path.isdir(decrypted_dir):
        return []
    out = []
    for sub in (os.path.join(decrypted_dir, 'message'), decrypted_dir):
        if not os.path.isdir(sub):
            continue
        try:
            names = os.listdir(sub)
        except OSError:
            # 坏源不能中断整次构建：子目录不可读时继续尝试父目录
            continue
        for name in names:
            if not _MSG_DB_RE.match(name):
                continue
            p = os.path.join(sub, name)
            if os.path.isfile(p) and p not in out:
                out.append(p)
        if out:
            break
    return sorted(os.path.abspath(p) for p in out)


def discover_fts_content_tables(conn):
    """动态发现微信 FTS 的 content 分表（数量随微信版本变化，勿硬编码）。

    LIKE 里的 `_` / `%` 是通配符，`message_fts_v4_%_content` 会误匹配
    `message_fts_v4_X_content`、`message_fts_v4__content` 之类；与 `_msg_tables`
    同一缺陷类，因此同样「LIKE 粗筛 + Python 精确校验」。Task 4 会拿这些表名做
    `INSERT … SELECT`，误收野表就会写错数据。
    """
    out = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name LIKE 'message_fts_v4_%_content' ORDER BY name"):
        if isinstance(name, bytes):
            name = name.decode('utf-8', 'replace')
        if _FTS_CONTENT_RE.match(name):
            out.append(name)
    return out


def _msg_tables(conn):
    """含 Msg_ 前缀的表。

    注意 SQL LIKE 里 `_` 是单字符通配符，所以 `LIKE 'Msg_%'` 会误匹配 `MsgX…`；
    这里按既有代码习惯用 LIKE 粗筛，再用 Python 精确校验前缀。
    """
    out = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
        if isinstance(name, bytes):
            name = name.decode('utf-8', 'replace')
        if name.startswith('Msg_'):
            out.append(name)
    return out


def _connect_ro(path):
    """以只读方式打开分片（唯一的构造点，勿在别处再写字面 URI）。

    必须用 pathlib 的 `as_uri()`，**绝不能字符串拼接** `'file:%s?mode=ro' % path`：
    含 `#` 的路径会在 fragment 处被截断，连 `?mode=ro` 一起丢掉 —— 连接变成
    **可写**、在真实分片旁落地 0 字节垃圾文件，而随后的读取失败又被调用方的
    `except sqlite3.Error` 吞掉，于是整个分片静默贡献 0 行（实测 chat_map 直接为 {}）；
    含 `%XX` 的路径还会被百分号解码成另一个路径。as_uri() 会正确转义
    （`#`→`%23`、`%`→`%25`），实测可正常打开。
    """
    return sqlite3.connect(pathlib.Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def build_chat_map(shards):
    """合并各分片 Name2Id → {md5(username): username}。

    Msg_<md5hash> 的表名即来源，因此这份映射是会话定位的唯一依据。
    """
    mapping = {}
    for shard in shards:
        try:
            conn = _connect_ro(shard)
        except sqlite3.Error:
            continue
        try:
            for (uname,) in conn.execute('SELECT user_name FROM Name2Id'):
                if not uname:
                    continue
                u = uname.decode('utf-8', 'replace') if isinstance(uname, bytes) else str(uname)
                if u:
                    mapping.setdefault(hashlib.md5(u.encode()).hexdigest(), u)
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return mapping


def create_schema(conn):
    """建表与索引。

    幂等**只对空库成立**：`CREATE TABLE IF NOT EXISTS` 对已存在的表名是整条 no-op，
    旧结构会原样留下（A1 的事故）。所以 `build_index` 必须先跑
    `_index_needs_rebuild` / `_migrate_schema`；本函数只负责「库是新的或已清干净」
    这个前提下的建表。

    ⚠️ msg_meta 的主键必须是 (chat_id, local_id, create_time)，**绝不能只用前两列**。
    实测本机真实数据：`local_id` 在**每个分片里都从 1 重数**，于是同一个
    (chat_id, local_id) 会在不同分片里指向**完全不同的消息** ——
      distinct(chat, local_id)              =   461,455  ← 只用这两列会折叠 573,837 行（丢 55.4% 消息）
      distinct(chat, local_id, create_time) = 1,035,292  ← 唯一 ✓
    221,678 个重复键中 create_time 全部不同（样本横跨 2021–2026），确实是不同消息被同键撞上；
    而同一分片内 (chat, local_id) 无重复，故加上 create_time 即唯一。
    该三列同时是与 message_fts / msg_text 的 join 键（两侧同样唯一，
    且 100% 落在消息侧同名键集合内）。
    """
    conn.executescript("""
CREATE TABLE IF NOT EXISTS msg_meta (
  chat_id          TEXT    NOT NULL,
  local_id         INTEGER NOT NULL,
  create_time      INTEGER NOT NULL,
  local_type       INTEGER NOT NULL,
  sender_username  TEXT,
  db_stem          TEXT,
  PRIMARY KEY (chat_id, local_id, create_time)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_meta_type_time ON msg_meta(local_type, create_time);
CREATE INDEX IF NOT EXISTS idx_meta_time      ON msg_meta(create_time);
CREATE INDEX IF NOT EXISTS idx_meta_sender    ON msg_meta(sender_username);
CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT);
""" + _MSG_TEXT_DDL)
    conn.commit()


_NO_META_STATS = {'rows': 0, 'skipped_unresolved_tables': 0,
                  'skipped_unresolved_rows': 0, 'skipped_shards': 0,
                  'skipped_unreadable_tables': 0}


def build_msg_meta_ex(conn, shards, chat_map, progress=None):
    """扫全部 Msg_ 表写入 msg_meta（只读数值列，不解压正文）。返回统计 dict。

    local_type 归一化为基类型（lt & 0xFFFFFFFF），与 MSG_TYPE_LABELS 语义一致。
    real_sender_id 是**分片内**的 Name2Id rowid，必须按分片解析。

    返回键（A3：静默丢弃必须可见 —— 若微信改了 Name2Id 的表名/列名/id 语义，
    整个会话的元数据会被跳过，而索引仍报 ready）：
      rows                      实际落库行数（COUNT(*)，非尝试写入数）
      skipped_unresolved_tables Msg_<md5> 的 md5 在 chat_map 里找不到 → 整表跳过
      skipped_unresolved_rows   上述表里的行数（只在真出现时才付 COUNT(*) 的代价）
      skipped_shards            打不开的分片数
      skipped_unreadable_tables 读不了的表数（表结构损坏等）
    """
    stats = dict(_NO_META_STATS)
    if progress:
        progress(STAGE_META, '清空旧元数据...', 0.0)
    conn.execute('DELETE FROM msg_meta')
    conn.commit()

    for idx, shard in enumerate(shards):
        stem = os.path.splitext(os.path.basename(shard))[0]
        try:
            src = _connect_ro(shard)
        except sqlite3.Error:
            stats['skipped_shards'] += 1
            continue
        try:
            senders = {}
            try:
                for rid, uname in src.execute('SELECT rowid, user_name FROM Name2Id'):
                    if uname:
                        senders[rid] = (uname.decode('utf-8', 'replace')
                                        if isinstance(uname, bytes) else str(uname))
            except sqlite3.Error:
                pass

            try:
                tables = _msg_tables(src)
            except sqlite3.Error:
                stats['skipped_shards'] += 1
                continue
            for table in tables:
                h = table[4:].lower() if isinstance(table, str) else table.decode()[4:].lower()
                chat = chat_map.get(h)
                if not chat:
                    # 会话名解析不出：整表丢弃。**必须计数**，否则这里就是
                    # 「索引 ready、该会话永远搜不到」的静默丢数据源头。
                    stats['skipped_unresolved_tables'] += 1
                    try:
                        stats['skipped_unresolved_rows'] += src.execute(
                            'SELECT COUNT(*) FROM [%s]' % table).fetchone()[0] or 0
                    except sqlite3.Error:
                        pass
                    continue
                try:
                    rows = src.execute(
                        'SELECT local_id, local_type, create_time, real_sender_id'
                        ' FROM [%s]' % table).fetchall()
                except sqlite3.Error:
                    stats['skipped_unreadable_tables'] += 1
                    continue
                if not rows:
                    continue
                conn.executemany(
                    'INSERT OR REPLACE INTO msg_meta'
                    ' (chat_id, local_id, local_type, create_time, sender_username, db_stem)'
                    ' VALUES (?,?,?,?,?,?)',
                    [(chat, lid or 0, (lt or 0) & 0xFFFFFFFF, ct or 0,
                      senders.get(sid), stem) for lid, lt, ct, sid in rows])
        finally:
            src.close()
        conn.commit()
        if progress:
            progress(STAGE_META, '已处理 %s' % stem,
                     (idx + 1) / max(len(shards), 1))
    stats['rows'] = conn.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0]
    return stats


def build_msg_meta(conn, shards, chat_map, progress=None):
    """msg_meta 的**实存**行数。全部计数见 `build_msg_meta_ex()`。"""
    return build_msg_meta_ex(conn, shards, chat_map, progress)['rows']


# ==========================================================================
# message_fts / msg_text 构建
# ==========================================================================

_FTS_INSERT_BATCH = 20000

# msg_meta 的主键 / 与两侧文本表的 join 键（三列，缺一列就会折叠消息）
PK_COLUMNS = ('chat_id', 'local_id', 'create_time')

_MSG_TEXT_DDL = """
CREATE TABLE IF NOT EXISTS msg_text (
  rowid       INTEGER PRIMARY KEY,
  chat_id     TEXT    NOT NULL,
  local_id    INTEGER NOT NULL,
  create_time INTEGER NOT NULL,
  raw_text    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_text_key
  ON msg_text(chat_id, local_id, create_time);
"""


def _fts_db_path(decrypted_dir):
    """微信 message_fts.db 的位置，候选顺序与 `discover_message_shards` **一致**。

    硬编码 `<解密目录>/message/message_fts.db` 在另一种布局（分片与 fts 库直接
    放在解密根目录）上会静默得到 0 行文本 —— 而「索引报告 ready、全文搜索永远
    0 结果」正是本方案最危险的失败态。
    """
    if not decrypted_dir:
        return ''
    for sub in (os.path.join(decrypted_dir, 'message'), decrypted_dir):
        p = os.path.join(sub, 'message_fts.db')
        if os.path.isfile(p):
            return os.path.abspath(p)
    return os.path.abspath(os.path.join(decrypted_dir, 'message', 'message_fts.db'))


def _create_fts_table(conn):
    """(重)建 message_fts 虚表。

    char-token + unicode61：trigram 要求 >=3 字符，中文双字词（"维修"）会全漏。
    三列 UNINDEXED 是为了 MATCH 命中的行**不必回 msg_meta** 就能定位。
    """
    conn.execute('DROP TABLE IF EXISTS message_fts')
    conn.execute("""
CREATE VIRTUAL TABLE message_fts USING fts5(
  text,
  chat_id UNINDEXED,
  local_id UNINDEXED,
  create_time UNINDEXED,
  tokenize = 'unicode61'
)""")


def _ensure_text_table(conn):
    conn.executescript(_MSG_TEXT_DDL)


def _read_fts_sessions(conn):
    """{name2id.rowid: username}。表名/列名/语义变了就返回 {}。

    返回 {} 会让每一行都落进 `skipped_no_chat`，由 A3 暴露给 UI；
    绝不静默返回 0 行 —— 那会被误读成「没有这条消息」。
    """
    out = {}
    try:
        for rid, uname in conn.execute('SELECT rowid, username FROM name2id'):
            if not uname:
                continue
            out[rid] = (uname.decode('utf-8', 'replace')
                        if isinstance(uname, (bytes, bytearray)) else str(uname))
    except sqlite3.Error:
        pass
    return out


def _flush_fts_batch(conn, buf_raw, buf_text):
    """两张表用**同一个显式 rowid** 写入（A8）。

    rowid 由调用方自己的计数器分配，不用 lastrowid：批量 executemany 拿不到
    逐行 rowid，而逐行插入 89 万次慢到不可接受。
    """
    conn.executemany(
        'INSERT OR IGNORE INTO msg_text'
        ' (rowid, chat_id, local_id, create_time, raw_text) VALUES (?,?,?,?,?)', buf_raw)
    conn.executemany(
        'INSERT INTO message_fts'
        ' (rowid, text, chat_id, local_id, create_time) VALUES (?,?,?,?,?)', buf_text)


def _count_text_without_meta(conn):
    """`msg_text` 里在 `msg_meta` 中找不到同三元组的行数。

    这正是**关键词内连接会丢掉的那些行**（Task 5 的 join 语义见报告 §7）：正文存在，
    但消息侧没有对应的 (chat_id, local_id, create_time)。真实数据实测 **125 行**
    （占正文行 0.014%，其中 124 行的 (chat, local_id) 在 msg_meta 里完全不存在）。

    为什么要单独统计：0.014% 不值得重构 join 语义，但**静默**不行 —— 若哪天微信改了
    content 表的会话或键语义，这个数字会变大，而症状只是「搜索少了一些结果」，
    正是本项目一直在堵的静默丢失。

    走 `msg_meta` 的三列主键（WITHOUT ROWID 全键查找），不是逐页扫描。
    """
    return conn.execute("""
SELECT COUNT(*) FROM msg_text t
WHERE NOT EXISTS (
  SELECT 1 FROM msg_meta m
  WHERE m.chat_id = t.chat_id AND m.local_id = t.local_id
    AND m.create_time = t.create_time)""").fetchone()[0]


def _drop_orphan_fts_rows(conn):
    """删掉 msg_text 里没有对应 rowid 的 FTS 行。

    只在源里出现**完全相同三元组**时才走到（msg_text 的唯一索引把它去重了，
    而 FTS5 虚表没有主键、两行都真的插进去了）。孤儿行会让 Task 5 的 join
    出现重复命中 + 摘要串行，所以必须清掉。正常数据零成本。
    """
    seen = {r[0] for r in conn.execute('SELECT rowid FROM msg_text')}
    orphans = [r[0] for r in conn.execute('SELECT rowid FROM message_fts')
               if r[0] not in seen]
    for start in range(0, len(orphans), 900):
        conn.executemany('DELETE FROM message_fts WHERE rowid=?',
                         [(i,) for i in orphans[start:start + 900]])
    conn.commit()
    return len(orphans)


def build_fts_ex(conn, fts_db_path, progress=None):
    """从微信 `message_fts_v4_*_content` 灌入 char-token 文本 + 原文。返回统计 dict。

    content 表列序：c0=text c1=local_id c2=sort_seq(ms) c3=local_type
                     c4=session_id c5=sender_id c6=create_time
    session_id 经 message_fts.db 自己的 name2id（rowid）映射为会话名。
    微信的 MATCH 不可用（私有分词器 MMFtsTokenizer），我们只把它的 content 当语料读。

    A8：原文**不**塞进 message_fts 的 UNINDEXED 列，而是独立普通表 `msg_text`，
    rowid 与 message_fts 一一对应。理由：FTS5 虚表的列**无法建 B 树索引** ——
    若原文只是 FTS 的 UNINDEXED 列，「无关键词的纯筛选查询」要显示摘要就得为每页
    ≤20 行各做一次 89 万行全扫（单列扫描 ~0.5–1s → 每页 10–20s，不可用），只能
    退化为「纯筛选结果一律无摘要」；而 spec 只承认「无正文消息无摘要」，并不承认
    文本消息也无摘要。加普通表 + UNIQUE 索引后 msg_meta LEFT JOIN msg_text 走索引。

    返回 dict（A3 静默丢弃可见 / A6 实存行数）：
      rows               message_fts 的**实存**行数（不是尝试写入数）
      text_rows          msg_text 的实存行数（正常与 rows 相等）
      skipped_no_chat    会话名解析不出而丢弃的行数
      skipped_empty_text 文本为空/纯空白而丢弃的行数
      duplicates_dropped 三元组完全相同被去重掉的行数
      tables             处理过的 content 表数（0 表示语料源本身就没找到）
    """
    stats = {'rows': 0, 'text_rows': 0, 'skipped_no_chat': 0,
             'skipped_empty_text': 0, 'duplicates_dropped': 0, 'tables': 0}
    if not fts_db_path or not os.path.isfile(fts_db_path):
        return stats
    if progress:
        progress(STAGE_TEXT, '读取微信文本语料...', 0.0)

    _create_fts_table(conn)
    _ensure_text_table(conn)
    conn.execute('DELETE FROM msg_text')
    conn.commit()

    try:
        src = _connect_ro(fts_db_path)
    except sqlite3.Error:
        return stats
    try:
        tables = discover_fts_content_tables(src)
        stats['tables'] = len(tables)
        if not tables:
            return stats
        sessions = _read_fts_sessions(src)
        counter = 0
        buf_raw = []
        buf_text = []
        for idx, table in enumerate(tables):
            try:
                cursor = src.execute('SELECT c0, c1, c4, c6 FROM [%s]' % table)
            except sqlite3.Error:
                continue
            for c0, c1, c4, c6 in cursor:
                chat = sessions.get(c4) if c4 is not None else None
                if not chat:
                    stats['skipped_no_chat'] += 1
                    continue
                if isinstance(c0, (bytes, bytearray)):
                    raw = c0.decode('utf-8', 'replace')
                elif c0 is None:
                    raw = ''
                else:
                    raw = c0 if isinstance(c0, str) else str(c0)
                tokens = to_char_token_text(raw)
                if not tokens:
                    stats['skipped_empty_text'] += 1
                    continue
                counter += 1
                buf_raw.append((counter, chat, c1 or 0, c6 or 0, raw))
                buf_text.append((counter, tokens, chat, c1 or 0, c6 or 0))
                if len(buf_raw) >= _FTS_INSERT_BATCH:
                    _flush_fts_batch(conn, buf_raw, buf_text)
                    buf_raw = []
                    buf_text = []
            if progress:
                progress(STAGE_TEXT, '已处理 %s' % table, (idx + 1) / len(tables))
        if buf_raw:
            _flush_fts_batch(conn, buf_raw, buf_text)
        conn.commit()
    finally:
        src.close()

    stats['rows'] = conn.execute('SELECT COUNT(*) FROM message_fts').fetchone()[0]
    stats['text_rows'] = conn.execute('SELECT COUNT(*) FROM msg_text').fetchone()[0]
    if stats['text_rows'] != stats['rows']:
        stats['duplicates_dropped'] = stats['rows'] - stats['text_rows']
        _drop_orphan_fts_rows(conn)
        stats['rows'] = conn.execute('SELECT COUNT(*) FROM message_fts').fetchone()[0]
    return stats


def build_fts(conn, fts_db_path, progress=None):
    """message_fts 的**实存**行数（A6）。全部计数见 `build_fts_ex()`。"""
    return build_fts_ex(conn, fts_db_path, progress)['rows']


def _clear_text_tables(conn):
    """text=False：清掉上一次构建的文本，而不是留着。

    留着会让后续 open_index 拿到的摘要与 msg_meta 来自两批不同的数据 —— 静默错数据。
    """
    _create_fts_table(conn)
    _ensure_text_table(conn)
    conn.execute('DELETE FROM msg_text')
    conn.commit()
    return {'rows': 0, 'text_rows': 0, 'skipped_no_chat': 0,
            'skipped_empty_text': 0, 'duplicates_dropped': 0, 'tables': 0}


# --------------------------------------------------------------------------
# schema 校验与迁移（A1）
# --------------------------------------------------------------------------

def _table_exists(conn, name):
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table','view')",
            (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _pk_columns(conn, table):
    """PRAGMA table_info → 主键列名集合（空集 = 表不存在或没有主键）。"""
    try:
        rows = conn.execute('PRAGMA table_info([%s])' % table).fetchall()
    except sqlite3.Error:
        return set()
    return {r[1] for r in rows if r[5]}


def _unique_index_ok(conn, index, table, columns):
    """索引存在、是 UNIQUE、且列序完全一致。"""
    try:
        found = [r for r in conn.execute('PRAGMA index_list([%s])' % table)
                 if r[1] == index and r[2]]
        if not found:
            return False
        cols = [r[2] for r in conn.execute('PRAGMA index_info([%s])' % index)]
    except sqlite3.Error:
        return False
    return cols == list(columns)


def _schema_ok(conn):
    """版本号 + **实际结构**双重校验。

    只信 `schema_version` 数字正是本次事故的根因：旧 2 列主键的库在 build_index
    跑完后会被写上新的版本号，于是「声明健康、实际丢了 55.4% 的消息」且此后永远
    检测不出来。所以这里额外校验 msg_meta 的**真实主键列集合**，以及 msg_text 与
    它的 UNIQUE 索引是否存在。
    """
    try:
        if str(_get_meta(conn, 'schema_version')) != str(SCHEMA_VERSION):
            return False
    except sqlite3.Error:
        return False
    if _pk_columns(conn, 'msg_meta') != set(PK_COLUMNS):
        return False
    if not _table_exists(conn, 'msg_text'):
        return False
    return _unique_index_ok(conn, 'idx_text_key', 'msg_text', PK_COLUMNS)


def _index_needs_rebuild(path):
    """既有索引库是否必须整库重建（A1）。

    两条判据缺一不可（本次事故的根因就是只信第一条）：
      1. 库不存在 → False（全新）；不可读/不是 sqlite/没有 schema_version/版本不等 → True；
      2. **实际结构**不符（msg_meta 主键列集合 != {chat_id, local_id, create_time}
         或 msg_text / idx_text_key 缺失）→ True。

    探测用**只读**连接：绝不在探测阶段创建文件。
    """
    if not os.path.isfile(path):
        return False
    try:
        conn = _connect_ro(path)
    except sqlite3.Error:
        return True
    try:
        return not _schema_ok(conn)
    except sqlite3.Error:
        return True
    finally:
        conn.close()


def _migrate_schema(conn):
    """清掉旧库的表结构，让 create_schema 真正重建（FTS 虚表先删）。"""
    conn.execute('DROP TABLE IF EXISTS message_fts')
    conn.execute('DROP TABLE IF EXISTS msg_meta')
    conn.execute('DROP TABLE IF EXISTS msg_text')
    conn.execute('DROP TABLE IF EXISTS index_meta')
    conn.commit()


def _prepare_index_db(path):
    """打开索引库；需要迁移时先清空旧结构，损坏到无法 DROP 就直接删库重建。"""
    needs_rebuild = _index_needs_rebuild(path)
    conn = sqlite3.connect(path)
    if not needs_rebuild:
        return conn
    try:
        _migrate_schema(conn)
        return conn
    except sqlite3.Error:
        # 文件不是 sqlite（或已损坏）→ DROP 都执行不了，直接删掉重建
        conn.close()
        for suffix in ('', '-wal', '-shm', '-journal'):
            try:
                os.remove(path + suffix)
            except OSError:
                pass
        return sqlite3.connect(path)


def _set_meta(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO index_meta (key, value) VALUES (?,?)',
                 (key, str(value)))


def _get_meta(conn, key, default=None):
    row = conn.execute('SELECT value FROM index_meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def _meta_int(conn, key, default=0):
    try:
        return int(_get_meta(conn, key) or 0)
    except (TypeError, ValueError, sqlite3.Error):
        return default


# --------------------------------------------------------------------------
# 源侧统计（只在构建时扫一次）与指纹（状态热路径）
# --------------------------------------------------------------------------

def _source_stats(shards):
    """源分片的 (消息总数, 最大 create_time)。

    ⚠️ 逐表 `COUNT(*)/MAX(create_time)`（真实数据 7 分片 / 4,675 张 Msg_ 表 /
    1,018,918 行）**只能在构建时**调用一次。实测这一段：全热缓存 1.40s、
    冷缓存 4.46–6.29s（控制方在更冷的机器上测得 13.4s）。原计划把它放进
    index_status 的热路径，而搜索页每次加载都会调 index_status —— 等于每次卡几秒；
    改成 stat 7 个分片后整个 index_status 只要 3.0–3.7ms。行数改为构建时算好、
    写进 index_meta，状态查询直接读。
    """
    rows = 0
    max_ct = 0
    for shard in shards:
        try:
            src = _connect_ro(shard)
        except sqlite3.Error:
            continue
        try:
            try:
                tables = _msg_tables(src)
            except sqlite3.Error:
                continue
            for table in tables:
                try:
                    n, mx = src.execute(
                        'SELECT COUNT(*), MAX(create_time) FROM [%s]' % table).fetchone()
                except sqlite3.Error:
                    continue
                rows += n or 0
                max_ct = max(max_ct, mx or 0)
        finally:
            src.close()
    return rows, max_ct


def _source_fingerprint(shards):
    """分片指纹：`basename:size:mtime_ns`（外加 `-wal` 的大小）拼串的 md5。

    这是 index_status 判 stale 的**唯一**依据。成本 = 7 次 os.stat（实测 1.4ms；
    整个 index_status 实测 3.0–3.7ms），对比全表扫描 1.40s（全热缓存）～13.4s（控制方冷机器）。
    用 `st_mtime_ns` 而非整数秒：同一秒内的写入也能察觉（NTFS 时间戳 100ns 粒度）。

    为什么必须带上 `-wal` 的大小（用真实分片的**副本**实测）：
    微信分片是 WAL 模式（目录里就有 `-wal`/`-shm`）。写者提交后若仍持有连接、
    尚未 checkpoint，`message_N.db` 的 **size 与 mtime 完全不变**，新消息只在 `-wal` 里：
        before write    db size=27013120 mtime_ns=...841000 | wal size=0
        after commit    db size=27013120 mtime_ns=...841000 | wal size=24752   <- .db 毫无变化
        (writer close)  checkpoint → .db mtime 变、-wal 消失
    只看 .db 的话，这类追加会被判成 stale=False —— 索引静默缺消息。

    ⚠️ 但**只认非空 `-wal`**（`st_size > 0`）。实测：**空的 `-wal` 由连接本身造出来、
    也由连接关闭删掉** —— 只读连接挂上分片就会留下一个 0 字节 `-wal`，事后再开一个读写
    连接并关闭，它连同 `-shm` 一起消失，而 `.db` 一动没动。若把 `-wal:0` 也算进指纹，
    真实索引会在「什么都没变」的情况下被判 stale（我第一版就是这个缺陷，实测真实索引
    `stale=True` 才发现：构建时 `-wal` 存在 0 字节，之后被删）。
    空 `-wal` 不携带任何数据信息，而**非空** `-wal` 才是「未 checkpoint 的写入」的信号；
    写入被 checkpoint 之后 `.db` 必然变化，所以去掉空 `-wal` 不丢任何检测能力。

    为什么**绝不能**把 `-shm` 放进指纹：它只服务 WAL-index 协作，**每次有连接挂上
    分片都会被写**（实测我们的只读连接就把真实分片的 `-shm` mtime 改了），
    放进去会让每次构建完的状态立刻变成 stale。
    """
    parts = []
    for shard in shards:
        try:
            st = os.stat(shard)
        except OSError:
            continue
        base = os.path.basename(shard)
        parts.append('%s:%d:%d' % (base, st.st_size, st.st_mtime_ns))
        try:
            wal = os.stat(shard + '-wal')
        except OSError:
            continue
        if wal.st_size > 0:                      # 空 -wal 是连接产物，不是数据变化
            parts.append('%s-wal:%d' % (base, wal.st_size))
    return hashlib.md5('|'.join(parts).encode()).hexdigest()


# --------------------------------------------------------------------------
# 全量构建 / 状态 / 刷新 / 打开
# --------------------------------------------------------------------------

def build_index(decrypted_dir, *, text=True, meta=True, force=False, progress=None):
    """全量构建（或重建）索引。返回 dict，键见模块文档与 A3/A8 的契约。

    force=False 时，若索引已经「正好是这次要的东西」（schema 匹配、不陈旧、
    且请求的部件都非空）就直接返回现状（`skipped=True`）；force=True 无条件重建。
    `refresh_index` 走的是 force=True 这条。
    """
    path = index_path(decrypted_dir)
    if not force:
        st = index_status(decrypted_dir)
        if (st['ready'] and not st['stale']
                and (not text or st['fts_rows'] > 0)
                and (not meta or st['meta_rows'] > 0)):
            return {'skipped': True, **st}

    if progress:
        progress(STAGE_META, '发现源数据...', 0.0)
    shards = discover_message_shards(decrypted_dir)
    chat_map = build_chat_map(shards)
    fts_db = _fts_db_path(decrypted_dir)

    # 指纹与行数在**读取之前**采集：构建期间源被改动的话，索引会被标记为 stale
    # （下次刷新自愈），而不是被标成「新鲜」却缺行。
    source_rows, source_max_ct = _source_stats(shards)
    fingerprint = _source_fingerprint(shards)

    conn = _prepare_index_db(path)          # A1：先迁移，再 create_schema
    try:
        create_schema(conn)
        meta_stats = (build_msg_meta_ex(conn, shards, chat_map, progress) if meta
                      else _NO_META_STATS)
        fts_stats = build_fts_ex(conn, fts_db, progress) if text else _clear_text_tables(conn)
        if text:
            if progress:
                progress(STAGE_OPTIMIZE, '优化全文索引...', 0.9)
            try:
                conn.execute("INSERT INTO message_fts(message_fts) VALUES('optimize')")
                conn.commit()
            except sqlite3.Error:
                pass

        built_at = int(time.time())
        # 「有正文、没有 msg_meta」的行数：关键词内连接会丢掉它们，必须可见（见函数注释）
        text_without_meta = _count_text_without_meta(conn)
        _set_meta(conn, 'schema_version', SCHEMA_VERSION)
        _set_meta(conn, 'built_at', built_at)
        _set_meta(conn, 'source_rows', source_rows)
        _set_meta(conn, 'source_max_create_time', source_max_ct)
        _set_meta(conn, 'source_fingerprint', fingerprint)
        _set_meta(conn, 'meta_rows', meta_stats['rows'])
        _set_meta(conn, 'fts_rows', fts_stats['rows'])
        _set_meta(conn, 'msg_text_rows', fts_stats['text_rows'])
        _set_meta(conn, 'fts_rows_without_meta', text_without_meta)
        _set_meta(conn, 'fts_skipped_no_chat', fts_stats['skipped_no_chat'])
        _set_meta(conn, 'fts_skipped_empty_text', fts_stats['skipped_empty_text'])
        _set_meta(conn, 'duplicates_dropped', fts_stats['duplicates_dropped'])
        _set_meta(conn, 'meta_skipped_unresolved_tables',
                  meta_stats['skipped_unresolved_tables'])
        _set_meta(conn, 'meta_skipped_unresolved_rows',
                  meta_stats['skipped_unresolved_rows'])
        _set_meta(conn, 'meta_skipped_shards', meta_stats['skipped_shards'])
        conn.commit()
        if progress:
            progress(STAGE_DONE, '完成', 1.0)
        return {
            'meta_rows': meta_stats['rows'],
            'fts_rows': fts_stats['rows'],
            'msg_text_rows': fts_stats['text_rows'],
            'fts_rows_without_meta': text_without_meta,
            'source_rows': source_rows,
            'built_at': built_at,
            'meta_skipped_unresolved_tables': meta_stats['skipped_unresolved_tables'],
            'meta_skipped_unresolved_rows': meta_stats['skipped_unresolved_rows'],
            'meta_skipped_shards': meta_stats['skipped_shards'],
            'fts_skipped_no_chat': fts_stats['skipped_no_chat'],
            'fts_skipped_empty_text': fts_stats['skipped_empty_text'],
            'duplicates_dropped': fts_stats['duplicates_dropped'],
        }
    finally:
        conn.close()


_STATUS_INT_KEYS = (
    'meta_rows', 'fts_rows', 'msg_text_rows', 'source_rows', 'source_max_create_time',
    'fts_skipped_no_chat', 'fts_skipped_empty_text', 'duplicates_dropped',
    'fts_rows_without_meta',
    'meta_skipped_unresolved_tables', 'meta_skipped_unresolved_rows',
    'meta_skipped_shards',
)


def index_status(decrypted_dir):
    """返回索引状态。`ready=False` 时调用方应走降级路径。

    热路径成本 = 1 次索引库只读连接 + 7 次 os.stat：真实数据实测 **3.0–3.7ms**。
    绝不逐表 COUNT(*)/MAX(create_time)（A4：同一台机器上实测 1.40s 全热缓存、
    4.46–6.29s 冷缓存，控制方在更冷的机器上测得 13.4s，见 `_source_stats`）。
    """
    path = index_path(decrypted_dir)
    st = {'exists': os.path.isfile(path), 'ready': False, 'stale': False,
          'schema_ok': False, 'built_at': None, 'source_shards': 0}
    for key in _STATUS_INT_KEYS:
        st[key] = 0
    st['meta_coverage'] = 0.0
    st['fts_coverage'] = 0.0
    if not st['exists']:
        return st

    try:
        conn = _connect_ro(path)
    except sqlite3.Error:
        return st
    try:
        st['schema_ok'] = _schema_ok(conn)
        for key in _STATUS_INT_KEYS:
            st[key] = _meta_int(conn, key)
        st['built_at'] = _meta_int(conn, 'built_at') or None
        st['ready'] = bool(st['schema_ok'] and (st['fts_rows'] or st['meta_rows']))
        if st['source_rows'] > 0:
            st['meta_coverage'] = round(min(st['meta_rows'] / st['source_rows'], 1.0), 4)
            st['fts_coverage'] = round(min(st['fts_rows'] / st['source_rows'], 1.0), 4)
        shards = discover_message_shards(decrypted_dir)
        st['source_shards'] = len(shards)
        if st['ready']:
            st['stale'] = (_source_fingerprint(shards)
                           != str(_get_meta(conn, 'source_fingerprint')))
        return st
    finally:
        conn.close()


def refresh_index(decrypted_dir, *, progress=None):
    """增量刷新：源未变则跳过；变了则整库重建（撤回/编辑会让真增量不可靠）。

    源分片一个都找不到、而现有索引有行时**拒绝重建**（`skipped=True` +
    `reason='no_source'`）：否则「解密盘没挂载 / 目录被清空」会把一份好索引
    悄悄换成空索引，用户看到的是「搜索没结果」，而不是「源不见了」。
    """
    st = index_status(decrypted_dir)
    if st['ready'] and not st['stale']:
        return {'skipped': True, **st}
    if st['ready'] and not st.get('source_shards'):
        return {'skipped': True, 'reason': 'no_source', **st}
    return build_index(decrypted_dir, force=True, progress=progress)


def open_index(decrypted_dir):
    """打开索引连接；不存在或不可读时返回 None（调用方走降级）。**只读**。

    必须用 `_connect_ro`（pathlib as_uri 转义）：手写 `'file:%s?mode=ro'` 在含
    `#` 的路径上会截断成另一个库并丢掉只读，在用户目录里落地垃圾文件。
    """
    path = index_path(decrypted_dir)
    if not os.path.isfile(path):
        return None
    try:
        return _connect_ro(path)
    except sqlite3.Error:
        return None
