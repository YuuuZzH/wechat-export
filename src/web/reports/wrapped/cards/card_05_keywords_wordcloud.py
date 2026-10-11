from __future__ import annotations

import hashlib
import logging
import math
import random
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from ..adapter import _decode_message_content, _decode_sqlite_text, _iter_message_db_paths, _quote_ident, get_account_wxid

logger = logging.getLogger(__name__)


_MD5_HEX_RE = re.compile(r"(?i)\b[0-9a-f]{32}\b")
_URL_RE = re.compile(r"(?i)\bhttps?://\S+")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HAS_CJK_RE = re.compile(r"[一-鿿]")
_CJK_SEQ_RE = re.compile(r"[一-鿿]+")
_HAS_ALNUM_RE = re.compile(r"[一-鿿A-Za-z0-9]")
_EN_WORD_RE = re.compile(r"^[A-Za-z]{3,16}$")
_DATEISH_RE = re.compile(
    r"^(?:"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|"
    r"\d{1,2}:\d{2}"
    r"|"
    r"\d{1,2}月\d{1,2}日"
    r")$"
)

_WEFLOW_COMMON_PHRASE_LOCAL_TYPES = (1, 244813135921)

_STOPWORDS_ZH = {
    "的", "了", "是", "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "它们",
    "这", "那", "这个", "那个", "这里", "那里", "这样", "那样",
    "就是", "也是", "还有", "因为", "所以", "但是", "如果", "然后", "已经", "可以", "还是", "可能", "不会", "没有", "不是",
    "一个", "一下", "一下子", "一下下",
    "哈哈", "哈哈哈", "嘿嘿", "呜呜",
    "嗯", "哦", "啊", "呀", "啦", "嘛", "呢", "吧", "额", "诶", "哇", "唉",
    "好", "行", "可以", "ok", "OK",
}

_STOPWORDS_EN = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "with", "at", "from", "as",
    "is", "are", "was", "were", "be", "been", "being",
    "i", "me", "my", "you", "your", "he", "she", "it", "we", "they", "them",
    "this", "that", "these", "those",
    "yeah", "haha", "ok", "okay", "pls", "lol",
}


def _year_range_epoch_seconds(year: int) -> tuple[int, int]:
    start = int(datetime(int(year), 1, 1).timestamp())
    end = int(datetime(int(year) + 1, 1, 1).timestamp())
    return start, end


def _stable_seed(account_name: str, year: int) -> int:
    s = f"{str(account_name or '').strip()}|{int(year)}|wrapped_keywords"
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _list_message_tables(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except Exception:
        return []
    names: list[str] = []
    for r in rows:
        if not r or not r[0]:
            continue
        name = _decode_sqlite_text(r[0]).strip()
        if not name:
            continue
        ln = name.lower()
        if ln.startswith(("msg_", "chat_")):
            names.append(name)
    return names


def _clean_text(text: str) -> str:
    s = str(text or "")
    if not s:
        return ""
    s = s.replace("​", "").replace("﻿", "")
    s = _CTRL_RE.sub("", s)
    s = _URL_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    if s.startswith("<") or s.startswith('"<'):
        return ""
    return s


def _is_good_bubble_text(text: str) -> bool:
    s = _clean_text(text)
    if not s:
        return False
    if len(s) < 2:
        return False
    if _URL_RE.search(s):
        return False
    if _MD5_HEX_RE.fullmatch(s.replace(" ", "")):
        return False
    if not re.search(r"[一-鿿A-Za-z]", s):
        return False
    if not _HAS_ALNUM_RE.search(s):
        return False
    if re.fullmatch(r"[0-9]+", s):
        return False
    return True


def _is_good_example_text(text: str) -> bool:
    s = _clean_text(text)
    if not s:
        return False
    if len(s) < 2:
        return False
    if _URL_RE.search(s):
        return False
    if _MD5_HEX_RE.search(s):
        return False
    if not re.search(r"[一-鿿A-Za-z]", s):
        return False
    return True


def _normalize_token(tok: str) -> str:
    s = str(tok or "").strip()
    if not s:
        return ""
    if len(s) > 32:
        return ""

    s = re.sub(r"^[^\w一-鿿]+|[^\w一-鿿]+$", "", s, flags=re.UNICODE).strip()
    if not s:
        return ""

    if _MD5_HEX_RE.fullmatch(s) or _MD5_HEX_RE.search(s):
        return ""
    if _DATEISH_RE.fullmatch(s):
        return ""

    if len(s) >= 18 and re.fullmatch(r"[A-Za-z0-9_-]+", s) and sum(ch.isdigit() for ch in s) >= 6:
        return ""

    if any(ch.isdigit() for ch in s):
        return ""

    has_cjk = bool(_HAS_CJK_RE.search(s))
    if has_cjk:
        if not (2 <= len(s) <= 8):
            return ""
        if s in _STOPWORDS_ZH:
            return ""
        return s

    if _EN_WORD_RE.fullmatch(s):
        low = s.lower()
        if low in _STOPWORDS_EN:
            return ""
        return low

    return ""




def pick_examples(
    keywords: list[dict[str, Any]],
    message_pool: list[str],
    *,
    per_word: int = 3,
) -> list[dict[str, Any]]:
    all_msgs = [_clean_text(x) for x in (message_pool or []) if _clean_text(x)]
    uniq_msgs = list(dict.fromkeys(all_msgs))
    out: list[dict[str, Any]] = []

    for kw in keywords:
        word = str(kw.get("word") or "").strip()
        if not word:
            continue
        count = int(kw.get("count") or 0)

        hits: list[str] = []
        limit = max(1, int(per_word))

        def _match(msg: str) -> bool:
            if not _is_good_example_text(msg):
                return False
            if _HAS_CJK_RE.search(word):
                return word in msg
            return word.lower() in msg.lower()

        for msg in uniq_msgs:
            if len(hits) >= limit:
                break
            if _match(msg):
                hits.append(msg)

        if len(hits) < limit:
            for msg in all_msgs:
                if len(hits) >= limit:
                    break
                if _match(msg):
                    hits.append(msg)

        out.append({"word": word, "count": int(count), "messages": hits})

    return out




def _weflow_common_phrase_or_empty(text: Any) -> str:
    s = str(text or "")
    if not s:
        return ""

    s = s.replace("​", "").replace("﻿", "")
    s = _CTRL_RE.sub("", s)
    s = s.strip()
    if not s:
        return ""

    if len(s) < 2 or len(s) > 20:
        return ""
    if "http" in s:
        return ""
    if "<" in s:
        return ""
    if s.startswith("[") or s.startswith("<?xml"):
        return ""
    return s


def build_common_phrases_payload(
    *,
    phrase_counts: Counter[str],
    seed: int,
    top_n: int = 32,
    bubble_limit: int = 180,
    example_texts: list[str] | None = None,
    examples_per_word: int = 3,
) -> dict[str, Any]:
    _ = seed

    items = [(p, int(c)) for p, c in (phrase_counts or {}).items() if int(c) >= 2]
    if not items:
        return {"topKeyword": None, "keywords": [], "bubbleMessages": [], "examples": []}

    items.sort(key=lambda kv: (-kv[1], kv[0]))
    items = items[: max(0, int(top_n or 0))]
    if not items:
        return {"topKeyword": None, "keywords": [], "bubbleMessages": [], "examples": []}

    vals = [math.sqrt(max(0, c)) for _, c in items]
    minv = min(vals) if vals else 0.0
    maxv = max(vals) if vals else 0.0

    keywords: list[dict[str, Any]] = []
    for (phrase, count), v in zip(items, vals):
        if maxv <= minv:
            weight = 1.0
        else:
            weight = 0.2 + 0.8 * ((v - minv) / (maxv - minv))
        keywords.append({"word": phrase, "count": int(count), "weight": round(float(weight), 4)})

    bubble_candidates = list(dict.fromkeys([str(p or "").strip() for p in phrase_counts.keys()]))
    bubble_candidates = [p for p in bubble_candidates if p]
    rnd = random.SystemRandom()
    rnd.shuffle(bubble_candidates)
    bubble_messages = bubble_candidates[: max(0, int(bubble_limit or 0))]

    if example_texts:
        per_word = max(1, int(examples_per_word or 1))
        examples = pick_examples(keywords, list(example_texts), per_word=per_word)
        for ex in examples:
            msgs = [str(m or "").strip() for m in (ex.get("messages") or []) if str(m or "").strip()]
            if not msgs:
                w = str(ex.get("word") or "").strip()
                ex["messages"] = [w] if w else []
            else:
                ex["messages"] = msgs[:per_word]
    else:
        examples = [{"word": kw["word"], "count": int(kw["count"]), "messages": [kw["word"]]} for kw in keywords]

    top_kw = {"word": str(keywords[0]["word"]), "count": int(keywords[0]["count"])} if keywords else None

    return {
        "topKeyword": top_kw,
        "keywords": keywords,
        "bubbleMessages": bubble_messages,
        "examples": examples,
    }


def _scan_common_phrase_counts(
    *,
    account_dir: Path,
    year: int,
    outgoing_only: bool,
    seed: int,
    max_seen: int | None = None,
) -> tuple[Counter[str], dict[str, Any]]:
    start_ts, end_ts = _year_range_epoch_seconds(int(year))
    _ = seed

    db_paths = _iter_message_db_paths(account_dir)
    db_paths = [p for p in db_paths if not p.name.lower().startswith("biz_message")]

    phrase_counts: Counter[str] = Counter()
    scanned = 0
    matched = 0
    capped = False

    t0 = time.time()
    for db_path in db_paths:
        if not db_path.exists():
            continue

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            conn.text_factory = bytes

            my_rowid: int | None = None
            if outgoing_only:
                try:
                    r = conn.execute(
                        "SELECT rowid FROM Name2Id WHERE user_name = ? LIMIT 1",
                        (get_account_wxid(account_dir),),
                    ).fetchone()
                    if r is not None and r[0] is not None:
                        my_rowid = int(r[0])
                except Exception:
                    my_rowid = None
                if my_rowid is None:
                    continue

            tables = _list_message_tables(conn)
            if not tables:
                continue
            tables.sort()

            ts_expr = (
                "CASE "
                "WHEN CAST(create_time AS INTEGER) > 1000000000000 "
                "THEN CAST(CAST(create_time AS INTEGER)/1000 AS INTEGER) "
                "ELSE CAST(create_time AS INTEGER) "
                "END"
            )

            local_types_csv = ",".join(str(int(x)) for x in _WEFLOW_COMMON_PHRASE_LOCAL_TYPES)

            for table in tables:
                if max_seen is not None and scanned >= int(max_seen):
                    capped = True
                    break

                qt = _quote_ident(table)
                where_sender = ""
                params: tuple[Any, ...]
                if outgoing_only and my_rowid is not None:
                    where_sender = " AND CAST(real_sender_id AS INTEGER) = ?"
                    params = (start_ts, end_ts, int(my_rowid))
                else:
                    params = (start_ts, end_ts)

                sql = (
                    "SELECT message_content, compress_content "
                    f"FROM {qt} "
                    f"WHERE CAST(local_type AS INTEGER) IN ({local_types_csv}) "
                    f"  AND {ts_expr} >= ? AND {ts_expr} < ?"
                    f"{where_sender}"
                )

                try:
                    cur = conn.execute(sql, params)
                except Exception:
                    continue

                for r in cur:
                    if max_seen is not None and scanned >= int(max_seen):
                        capped = True
                        break

                    scanned += 1
                    try:
                        raw_txt = _decode_message_content(r["compress_content"], r["message_content"])
                    except Exception:
                        continue

                    phrase = _weflow_common_phrase_or_empty(raw_txt)
                    if not phrase:
                        continue
                    phrase_counts[phrase] += 1
                    matched += 1
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        if max_seen is not None and scanned >= int(max_seen):
            break

    elapsed = time.time() - t0
    meta = {
        "scannedCandidates": int(scanned),
        "matchedCandidates": int(matched),
        "uniquePhrases": int(len(phrase_counts)),
        "capped": bool(capped),
        "elapsedSec": round(float(elapsed), 3),
        "localTypes": list(_WEFLOW_COMMON_PHRASE_LOCAL_TYPES),
    }
    return phrase_counts, meta


def _scan_message_pool(
    *,
    account_dir: Path,
    year: int,
    outgoing_only: bool,
    seed: int,
    max_pool: int = 3000,
    max_seen: int = 120_000,
) -> tuple[list[str], dict[str, Any]]:
    start_ts, end_ts = _year_range_epoch_seconds(int(year))
    _ = seed
    rnd = random.SystemRandom()

    db_paths = _iter_message_db_paths(account_dir)
    db_paths = [p for p in db_paths if not p.name.lower().startswith("biz_message")]
    rnd.shuffle(db_paths)

    pool: list[str] = []
    seen = 0

    t0 = time.time()
    for db_path in db_paths:
        if not db_path.exists():
            continue

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            conn.text_factory = bytes

            my_rowid: int | None = None
            if outgoing_only:
                try:
                    r = conn.execute(
                        "SELECT rowid FROM Name2Id WHERE user_name = ? LIMIT 1",
                        (get_account_wxid(account_dir),),
                    ).fetchone()
                    if r is not None and r[0] is not None:
                        my_rowid = int(r[0])
                except Exception:
                    my_rowid = None
                if my_rowid is None:
                    continue

            tables = _list_message_tables(conn)
            if not tables:
                continue
            rnd.shuffle(tables)

            ts_expr = (
                "CASE "
                "WHEN CAST(create_time AS INTEGER) > 1000000000000 "
                "THEN CAST(CAST(create_time AS INTEGER)/1000 AS INTEGER) "
                "ELSE CAST(create_time AS INTEGER) "
                "END"
            )

            for table in tables:
                if seen >= int(max_seen):
                    break
                qt = _quote_ident(table)
                where_sender = ""
                params: tuple[Any, ...]
                if outgoing_only and my_rowid is not None:
                    where_sender = " AND CAST(real_sender_id AS INTEGER) = ?"
                    params = (start_ts, end_ts, int(my_rowid))
                else:
                    params = (start_ts, end_ts)
                sql = (
                    "SELECT message_content, compress_content "
                    f"FROM {qt} "
                    "WHERE CAST(local_type AS INTEGER) = 1 "
                    f"  AND {ts_expr} >= ? AND {ts_expr} < ?"
                    f"{where_sender}"
                )

                try:
                    cur = conn.execute(sql, params)
                except Exception:
                    continue

                for r in cur:
                    if seen >= int(max_seen):
                        break
                    raw_txt = ""
                    try:
                        raw_txt = _decode_message_content(r["compress_content"], r["message_content"]).strip()
                    except Exception:
                        raw_txt = ""
                    cleaned = _clean_text(raw_txt)
                    if not cleaned:
                        continue
                    seen += 1

                    if len(pool) < int(max_pool):
                        pool.append(cleaned)
                        continue

                    j = rnd.randrange(seen)
                    if j < int(max_pool):
                        pool[j] = cleaned
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        if seen >= int(max_seen):
            break

    elapsed = time.time() - t0
    meta = {
        "scannedMessages": int(seen),
        "sampledMessages": int(len(pool)),
        "sampleRate": round(float(len(pool)) / float(seen), 6) if seen > 0 else 0.0,
        "elapsedSec": round(float(elapsed), 3),
    }
    return pool, meta


def build_card_05_keywords_wordcloud(*, account_dir: Path, year: int) -> dict[str, Any]:
    title = "这一年，你把哪些话说了一遍又一遍？"
    seed = _stable_seed(str(account_dir.name or ""), int(year))

    phrase_counts, scan_meta = _scan_common_phrase_counts(
        account_dir=account_dir,
        year=year,
        outgoing_only=True,
        seed=seed,
    )
    if int(scan_meta.get("scannedCandidates") or 0) <= 0:
        phrase_counts, scan_meta = _scan_common_phrase_counts(
            account_dir=account_dir,
            year=year,
            outgoing_only=False,
            seed=seed ^ 0x1234,
        )
        scan_meta["outgoingOnlyFallback"] = True

    example_pool: list[str] = []
    pool_meta: dict[str, Any] = {}
    if phrase_counts:
        use_outgoing_only = not bool(scan_meta.get("outgoingOnlyFallback") or False)
        example_pool, pool_meta = _scan_message_pool(
            account_dir=account_dir,
            year=year,
            outgoing_only=use_outgoing_only,
            seed=seed ^ 0x9E37,
            max_pool=3000,
            max_seen=120_000,
        )
        if (not example_pool) and use_outgoing_only:
            example_pool, pool_meta = _scan_message_pool(
                account_dir=account_dir,
                year=year,
                outgoing_only=False,
                seed=seed ^ 0xA53C,
                max_pool=3000,
                max_seen=120_000,
            )
            pool_meta["outgoingOnlyFallback"] = True

    payload = build_common_phrases_payload(
        phrase_counts=phrase_counts,
        seed=seed,
        example_texts=example_pool,
        examples_per_word=3,
    )

    logger.info(
        "Wrapped card#6 common phrases computed: account=%s year=%s phrases=%s bubble=%s scanned=%s matched=%s capped=%s elapsed=%.2fs",
        get_account_wxid(account_dir),
        int(year),
        len(payload.get("keywords") or []),
        len(payload.get("bubbleMessages") or []),
        int(scan_meta.get("scannedCandidates") or 0),
        int(scan_meta.get("matchedCandidates") or 0),
        bool(scan_meta.get("capped") or False),
        float(scan_meta.get("elapsedSec") or 0.0),
    )

    return {
        "id": 6,
        "title": title,
        "scope": "global",
        "category": "C",
        "status": "ok",
        "kind": "text/keywords_wordcloud",
        "narrative": "你的年度常用语词云",
        "data": {
            "year": int(year),
            **payload,
            "meta": {
                "scannedCandidates": int(scan_meta.get("scannedCandidates") or 0),
                "matchedCandidates": int(scan_meta.get("matchedCandidates") or 0),
                "uniquePhrases": int(scan_meta.get("uniquePhrases") or 0),
                "capped": bool(scan_meta.get("capped") or False),
                "localTypes": list(scan_meta.get("localTypes") or []),
                "outgoingOnlyFallback": bool(scan_meta.get("outgoingOnlyFallback") or False),
                "examplePoolScannedMessages": int(pool_meta.get("scannedMessages") or 0),
                "examplePoolSampledMessages": int(pool_meta.get("sampledMessages") or 0),
                "examplePoolOutgoingOnlyFallback": bool(pool_meta.get("outgoingOnlyFallback") or False),
            },
        },
    }
