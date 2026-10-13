from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .adapter import _decode_sqlite_text, _iter_message_db_paths, _quote_ident, resolve_account_dir, get_account_wxid, get_chat_search_index_db_path
from .storage import wrapped_cache_dir, wrapped_cache_path
from .cards.card_00_global_overview import build_card_00_global_overview
from .cards.card_01_cyber_schedule import WeekdayHourHeatmap, build_card_01_cyber_schedule, compute_weekday_hour_heatmap
from .cards.card_02_message_chars import build_card_02_message_chars
from .cards.card_05_keywords_wordcloud import build_card_05_keywords_wordcloud
from .cards.card_03_reply_speed import build_card_03_reply_speed
from .cards.card_04_monthly_best_friends_wall import build_card_04_monthly_best_friends_wall
from .cards.card_04_emoji_universe import build_card_04_emoji_universe
from .cards.card_07_bento_summary import build_card_07_bento_summary_from_sources

import logging
logger = logging.getLogger(__name__)


# We use this number to version the cache filename so adding more cards won't accidentally serve
# an older partial cache.
_IMPLEMENTED_UPTO_ID = 7
# Bump this when we change card payloads/ordering while keeping the same implemented_upto.
_CACHE_VERSION = 27


# "Manifest" is used by the frontend to render the deck quickly, then lazily fetch each card.
# Keep this list in display order (same as the old monolithic `/api/wrapped/annual` response).
_WRAPPED_CARD_MANIFEST: tuple[dict[str, Any], ...] = (
    {
        "id": 0,
        "title": "这一年，你的微信都经历了什么？",
        "scope": "global",
        "category": "A",
        "kind": "global/overview",
    },
    {
        "id": 1,
        "title": "你是「早八人」还是「夜猫子」？",
        "scope": "global",
        "category": "A",
        "kind": "time/weekday_hour_heatmap",
    },
    {
        "id": 2,
        "title": "你今年打了多少字？够写一本书吗？",
        "scope": "global",
        "category": "C",
        "kind": "text/message_chars",
    },
    {
        "id": 6,
        "title": "这一年，你把哪些词说了一遍又一遍？",
        "scope": "global",
        "category": "C",
        "kind": "text/keywords_wordcloud",
    },
    {
        "id": 3,
        "title": "谁是你「秒回」的置顶关心？",
        "scope": "global",
        "category": "B",
        "kind": "chat/reply_speed",
    },
    {
        "id": 4,
        "title": "这一年，每个月谁最懂你？",
        "scope": "global",
        "category": "B",
        "kind": "chat/monthly_best_friends_wall",
    },
    {
        "id": 5,
        "title": "这一年，你的表情包里藏了多少心情？",
        "scope": "global",
        "category": "B",
        "kind": "emoji/annual_universe",
    },
    {
        "id": 7,
        "title": "便当总览：一屏看完这一年",
        "scope": "global",
        "category": "A",
        "kind": "global/bento_summary",
    },
)
_WRAPPED_CARD_ID_SET = {int(c["id"]) for c in _WRAPPED_CARD_MANIFEST}


# Prevent duplicated heavy computations when multiple card endpoints are hit concurrently.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _get_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _default_year() -> int:
    return datetime.now().year


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
        if ln.startswith("msg_"):
            names.append(name)
    return names


def list_wrapped_available_years(*, account_dir: Path) -> list[int]:
    """List years that have *any* chat messages for the account (best-effort).

    Prefer using `chat_search_index.db` (fast). If not available, fall back to scanning message
    shard databases (slower, but works without the index).
    """

    # Try a tiny cache first (years don't change often, but scanning can be expensive).
    cache_path = wrapped_cache_dir(account_dir) / "available_years.json"
    max_mtime = 0
    try:
        index_path = get_chat_search_index_db_path(account_dir)
        if index_path.exists():
            max_mtime = max(max_mtime, int(index_path.stat().st_mtime))
    except Exception:
        pass
    try:
        for p in _iter_message_db_paths(account_dir):
            try:
                if p.name.lower().startswith("biz_message"):
                    continue
                if p.exists():
                    max_mtime = max(max_mtime, int(p.stat().st_mtime))
            except Exception:
                continue
    except Exception:
        pass

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                sig = int(cached.get("max_mtime") or 0)
                years = cached.get("years")
                if sig == max_mtime and isinstance(years, list):
                    out: list[int] = []
                    for x in years:
                        try:
                            y = int(x)
                        except Exception:
                            continue
                        if y > 0:
                            out.append(y)
                    out.sort(reverse=True)
                    return out
        except Exception:
            pass

    # Convert millisecond timestamps defensively (some datasets store ms).
    # The expression yields epoch seconds as INTEGER.
    ts_expr = (
        "CASE "
        "WHEN CAST(create_time AS INTEGER) > 1000000000000 "
        "THEN CAST(CAST(create_time AS INTEGER)/1000 AS INTEGER) "
        "ELSE CAST(create_time AS INTEGER) "
        "END"
    )

    # Fast path: use our unified search index when available.
    index_path = get_chat_search_index_db_path(account_dir)
    if index_path.exists():
        conn = sqlite3.connect(str(index_path))
        try:
            has_fts = (
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_fts' LIMIT 1").fetchone()
                is not None
            )
            if has_fts:
                sql = (
                    "SELECT "
                    "CAST(strftime('%Y', datetime(ts, 'unixepoch', 'localtime')) AS INTEGER) AS y, "
                    "COUNT(1) AS cnt "
                    "FROM ("
                    f"  SELECT {ts_expr} AS ts"
                    "  FROM message_fts"
                    f"  WHERE {ts_expr} > 0"
                    "    AND db_stem NOT LIKE 'biz_message%'"
                    ") sub "
                    "GROUP BY y "
                    "HAVING cnt > 0 "
                    "ORDER BY y DESC"
                )
                try:
                    rows = conn.execute(sql).fetchall()
                except Exception:
                    rows = []
                years: list[int] = []
                for r in rows:
                    if not r:
                        continue
                    try:
                        y = int(r[0])
                        cnt = int(r[1] or 0)
                    except Exception:
                        continue
                    if y > 0 and cnt > 0:
                        years.append(y)
                years.sort(reverse=True)
                try:
                    cache_path.write_text(
                        json.dumps({"max_mtime": max_mtime, "years": years}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                return years
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # Fallback: scan message shard DBs (may be slow on very large datasets, but only runs
    # when the index does not exist).
    year_counts: dict[int, int] = {}
    db_paths = _iter_message_db_paths(account_dir)
    db_paths = [p for p in db_paths if not p.name.lower().startswith("biz_message")]
    for db_path in db_paths:
        if not db_path.exists():
            continue
        conn = sqlite3.connect(str(db_path))
        try:
            tables = _list_message_tables(conn)
            if not tables:
                continue
            for table_name in tables:
                qt = _quote_ident(table_name)
                sql = (
                    "SELECT "
                    "CAST(strftime('%Y', datetime(ts, 'unixepoch', 'localtime')) AS INTEGER) AS y, "
                    "COUNT(1) AS cnt "
                    "FROM ("
                    f"  SELECT {ts_expr} AS ts"
                    f"  FROM {qt}"
                    f"  WHERE {ts_expr} > 0"
                    ") sub "
                    "GROUP BY y"
                )
                try:
                    rows = conn.execute(sql).fetchall()
                except Exception:
                    continue
                for r in rows:
                    if not r:
                        continue
                    try:
                        y = int(r[0])
                        cnt = int(r[1] or 0)
                    except Exception:
                        continue
                    if y > 0 and cnt > 0:
                        year_counts[y] = int(year_counts.get(y, 0)) + cnt
        finally:
            try:
                conn.close()
            except Exception:
                pass

    years = [y for y, cnt in year_counts.items() if int(cnt) > 0]
    years.sort(reverse=True)
    try:
        cache_path.write_text(
            json.dumps({"max_mtime": max_mtime, "years": years}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return years


def build_wrapped_annual_response(
    *,
    account: Optional[str],
    year: Optional[int],
    refresh: bool = False,
    chat: Optional[str] = None,
) -> dict[str, Any]:
    """Build annual wrapped response for the given account/year.

    For now we implement cards up to id=7 (plus a meta overview card id=0).
    When `chat` (username / 显示名) is set, build a per-session depth report instead.
    """

    account_dir = resolve_account_dir(account)

    available_years = list_wrapped_available_years(account_dir=account_dir)

    # If the requested year has no messages, snap to the latest available year so the selector only
    # shows years with data.
    requested_year = int(year or _default_year())
    y = requested_year
    if available_years and y not in available_years:
        y = int(available_years[0])

    chat_key = (chat or '').strip()
    if chat_key:
        return build_session_wrapped_response(
            account_dir=account_dir,
            year=y,
            requested_year=requested_year,
            chat=chat_key,
            available_years=available_years,
            refresh=refresh,
        )

    scope = "global"

    cache_path = wrapped_cache_path(
        account_dir=account_dir,
        scope=scope,
        year=y,
        implemented_upto=_IMPLEMENTED_UPTO_ID,
        options_tag=f"v{_CACHE_VERSION}",
    )
    if refresh:
        # Invalidate per-card caches so lazy-loaded cards reflect refreshed data
        for cid in _WRAPPED_CARD_ID_SET:
            card_cache = _wrapped_card_cache_path(account_dir=account_dir, scope=scope, year=y, card_id=cid)
            try:
                card_cache.unlink(missing_ok=True)
            except OSError:
                pass

    if (not refresh) and cache_path.exists():
        try:
            cached_obj = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached_obj, dict) and isinstance(cached_obj.get("cards"), list):
                # Card#6（关键词词云）要求每次请求返回随机消息批次，不复用旧卡片内容。
                for idx, c in enumerate(cached_obj.get("cards") or []):
                    try:
                        if int((c or {}).get("id") or -1) != 6:
                            continue
                    except Exception:
                        continue
                    cached_obj["cards"][idx] = build_card_05_keywords_wordcloud(account_dir=account_dir, year=y)
                    break
                cached_obj["cached"] = True
                cached_obj["availableYears"] = available_years
                return cached_obj
        except Exception:
            pass

    lock = _get_lock(str(cache_path))
    with lock:
        cards: list[dict[str, Any]] = []
        # Wrapped cards default to "messages sent by me" (outgoing), to avoid mixing directions
        # in first-person narratives like "你最常...".
        heatmap_sent = _get_or_compute_heatmap_sent(account_dir=account_dir, scope=scope, year=y, refresh=refresh)
        # Page 2: global overview (page 1 is the frontend cover slide).
        card_overview = build_card_00_global_overview(account_dir=account_dir, year=y, heatmap=heatmap_sent)
        cards.append(card_overview)
        # Page 3: cyber schedule heatmap.
        card_heatmap = build_card_01_cyber_schedule(account_dir=account_dir, year=y, heatmap=heatmap_sent)
        cards.append(card_heatmap)
        # Page 4: message char counts (sent vs received).
        card_message_chars = build_card_02_message_chars(account_dir=account_dir, year=y)
        cards.append(card_message_chars)
        # Page 5: annual keywords (bubble storm -> word cloud).
        cards.append(build_card_05_keywords_wordcloud(account_dir=account_dir, year=y))
        # Page 6: reply speed / best chat buddy.
        card_reply_speed = build_card_03_reply_speed(account_dir=account_dir, year=y)
        cards.append(card_reply_speed)
        # Page 7: monthly best friends wall (photo wall).
        card_monthly = build_card_04_monthly_best_friends_wall(account_dir=account_dir, year=y)
        cards.append(card_monthly)
        # Page 8: annual emoji universe / meme almanac.
        card_emoji = build_card_04_emoji_universe(account_dir=account_dir, year=y)
        cards.append(card_emoji)
        # Page 9: bento summary (prototype). Build from prior cards for consistency.
        cards.append(
            build_card_07_bento_summary_from_sources(
                year=y,
                overview=card_overview,
                heatmap=card_heatmap,
                message_chars=card_message_chars,
                reply_speed=card_reply_speed,
                monthly=card_monthly,
                emoji=card_emoji,
            )
        )

    obj: dict[str, Any] = {
        "account": account_dir.name,
        "year": y,
        "year_adjusted": y != requested_year,
        "requested_year": requested_year,
        "scope": scope,
        "username": get_account_wxid(account_dir),
        "generated_at": int(time.time()),
        "cached": False,
        "availableYears": available_years,
        "cards": cards,
    }

    try:
        cache_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to write wrapped cache: %s", cache_path)

    return obj


def build_wrapped_annual_meta(
    *,
    account: Optional[str],
    year: Optional[int],
    refresh: bool = False,
) -> dict[str, Any]:
    """Return a light-weight manifest for the Wrapped annual deck.

    This is meant to be fast so the frontend can render the deck first, then
    request each page (card) lazily to avoid freezing on initial load.
    """

    account_dir = resolve_account_dir(account)

    available_years = list_wrapped_available_years(account_dir=account_dir)

    # Keep the same year snapping semantics as `build_wrapped_annual_response`.
    requested_year = int(year or _default_year())
    y = requested_year
    if available_years and y not in available_years:
        y = int(available_years[0])

    if refresh:
        # The manifest itself is static today, but we keep the flag for API symmetry.
        pass

    return {
        "account": account_dir.name,
        "year": y,
        "year_adjusted": y != requested_year,
        "requested_year": requested_year,
        "scope": "global",
        "availableYears": available_years,
        # Shallow copy so callers can't mutate our module-level tuple.
        "cards": [dict(c) for c in _WRAPPED_CARD_MANIFEST],
    }


def _wrapped_cache_suffix() -> str:
    return f"_v{_CACHE_VERSION}"


def _wrapped_card_cache_path(*, account_dir: Path, scope: str, year: int, card_id: int) -> Path:
    # Keep stable names; per-account directory already namespaces the files.
    return wrapped_cache_dir(account_dir) / f"{scope}_{year}_card_{card_id}{_wrapped_cache_suffix()}.json"


def _wrapped_heatmap_sent_cache_path(*, account_dir: Path, scope: str, year: int) -> Path:
    return wrapped_cache_dir(account_dir) / f"{scope}_{year}_heatmap_sent{_wrapped_cache_suffix()}.json"


def _load_cached_heatmap_sent(path: Path) -> WeekdayHourHeatmap | None:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(obj, dict):
        return None

    weekday_labels = obj.get("weekdayLabels")
    hour_labels = obj.get("hourLabels")
    matrix = obj.get("matrix")
    total = obj.get("totalMessages")

    if not isinstance(weekday_labels, list) or not isinstance(hour_labels, list) or not isinstance(matrix, list):
        return None

    try:
        total_i = int(total or 0)
    except Exception:
        total_i = 0

    # Best-effort sanitize matrix to ints; keep shape if possible.
    out_matrix: list[list[int]] = []
    for row in matrix:
        if not isinstance(row, list):
            return None
        out_row: list[int] = []
        for v in row:
            try:
                out_row.append(int(v or 0))
            except Exception:
                out_row.append(0)
        out_matrix.append(out_row)

    return WeekdayHourHeatmap(
        weekday_labels=[str(x) for x in weekday_labels],
        hour_labels=[str(x) for x in hour_labels],
        matrix=out_matrix,
        total_messages=total_i,
    )


def _get_or_compute_heatmap_sent(*, account_dir: Path, scope: str, year: int, refresh: bool) -> WeekdayHourHeatmap:
    path = _wrapped_heatmap_sent_cache_path(account_dir=account_dir, scope=scope, year=year)
    lock = _get_lock(str(path))
    with lock:
        if not refresh:
            cached = _load_cached_heatmap_sent(path)
            if cached is not None:
                return cached

        heatmap = compute_weekday_hour_heatmap(account_dir=account_dir, year=year, sender_username=get_account_wxid(account_dir))
        try:
            path.write_text(
                json.dumps(
                    {
                        "weekdayLabels": heatmap.weekday_labels,
                        "hourLabels": heatmap.hour_labels,
                        "matrix": heatmap.matrix,
                        "totalMessages": heatmap.total_messages,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to write wrapped heatmap cache: %s", path)
        return heatmap


def build_wrapped_annual_card(
    *,
    account: Optional[str],
    year: Optional[int],
    card_id: int,
    refresh: bool = False,
) -> dict[str, Any]:
    """Build one Wrapped card (page) on-demand.

    The result is cached per account/year/card_id to avoid recomputing when users
    flip back and forth between pages.
    """

    cid = int(card_id)
    if cid not in _WRAPPED_CARD_ID_SET:
        raise ValueError(f"Unknown Wrapped card id: {cid}")

    account_dir = resolve_account_dir(account)

    available_years = list_wrapped_available_years(account_dir=account_dir)
    y = int(year or _default_year())
    if available_years and y not in available_years:
        y = int(available_years[0])

    scope = "global"
    cache_path = _wrapped_card_cache_path(account_dir=account_dir, scope=scope, year=y, card_id=cid)
    # Card#6 需要每次随机抽样，不使用按卡片缓存。
    cacheable = cid != 6

    lock = _get_lock(str(cache_path))
    with lock:
        if cacheable and (not refresh) and cache_path.exists():
            try:
                cached_obj = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached_obj, dict) and int(cached_obj.get("id") or -1) == cid:
                    return cached_obj
            except Exception:
                pass

        heatmap_sent: WeekdayHourHeatmap | None = None
        if cid in (0, 1):
            heatmap_sent = _get_or_compute_heatmap_sent(account_dir=account_dir, scope=scope, year=y, refresh=refresh)

        if cid == 0:
            card = build_card_00_global_overview(account_dir=account_dir, year=y, heatmap=heatmap_sent)
        elif cid == 1:
            card = build_card_01_cyber_schedule(account_dir=account_dir, year=y, heatmap=heatmap_sent)
        elif cid == 2:
            card = build_card_02_message_chars(account_dir=account_dir, year=y)
        elif cid == 6:
            card = build_card_05_keywords_wordcloud(account_dir=account_dir, year=y)
        elif cid == 3:
            card = build_card_03_reply_speed(account_dir=account_dir, year=y)
        elif cid == 4:
            card = build_card_04_monthly_best_friends_wall(account_dir=account_dir, year=y)
        elif cid == 5:
            card = build_card_04_emoji_universe(account_dir=account_dir, year=y)
        elif cid == 7:
            # Build from already-implemented cards so we can reuse their caches if available.
            overview = build_wrapped_annual_card(account=account_dir.name, year=y, card_id=0, refresh=refresh)
            heatmap = build_wrapped_annual_card(account=account_dir.name, year=y, card_id=1, refresh=refresh)
            message_chars = build_wrapped_annual_card(account=account_dir.name, year=y, card_id=2, refresh=refresh)
            reply_speed = build_wrapped_annual_card(account=account_dir.name, year=y, card_id=3, refresh=refresh)
            monthly = build_wrapped_annual_card(account=account_dir.name, year=y, card_id=4, refresh=refresh)
            emoji = build_wrapped_annual_card(account=account_dir.name, year=y, card_id=5, refresh=refresh)
            card = build_card_07_bento_summary_from_sources(
                year=y,
                overview=overview,
                heatmap=heatmap,
                message_chars=message_chars,
                reply_speed=reply_speed,
                monthly=monthly,
                emoji=emoji,
            )
        else:
            # Should be unreachable due to _WRAPPED_CARD_ID_SET check.
            raise ValueError(f"Unknown Wrapped card id: {cid}")

        if cacheable:
            try:
                cache_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                logger.exception("Failed to write wrapped card cache: %s", cache_path)

        return card


def build_session_wrapped_response(
    *,
    account_dir: Path,
    year: int,
    requested_year: int,
    chat: str,
    available_years: list[int],
    refresh: bool = False,
) -> dict[str, Any]:
    """Per-chat Wrapped：概览 + 热力 + 关键词（限定单个会话）。"""
    import hashlib
    from collections import Counter
    from datetime import datetime as _dt

    from engine.constants import TZ, DOW_NAMES

    tip = (chat or '').strip()
    if not tip:
        raise ValueError('缺少会话参数 chat')

    # Resolve chat_info from decrypted account dir
    decrypted = str(account_dir)
    chat_info = None
    try:
        from chat_list import scan_chats
        all_chats, _, _ = scan_chats(decrypted)
    except Exception:
        all_chats = []
    if not all_chats:
        # flat layout fallback via export_api helper pattern
        try:
            from web.routes.export_api import _scan_chats_flat
            all_chats = _scan_chats_flat(decrypted, tip)
        except Exception:
            all_chats = []

    tip_l = tip.lower()
    for c in all_chats or []:
        if c.get('username') == tip or c.get('display_name') == tip:
            chat_info = c
            break
    if not chat_info:
        fuzzy = [c for c in (all_chats or [])
                 if tip_l in (c.get('display_name') or '').lower()
                 or tip_l in (c.get('username') or '').lower()]
        if len(fuzzy) == 1:
            chat_info = fuzzy[0]
        elif not fuzzy:
            raise ValueError('未找到会话：%s' % tip)
        else:
            raise ValueError('匹配到多个会话，请使用精确 username：%s'
                             % ', '.join(c.get('username') or '?' for c in fuzzy[:8]))

    uname = chat_info.get('username') or tip
    display = chat_info.get('display_name') or uname
    scope = 'chat_' + hashlib.md5(uname.encode('utf-8')).hexdigest()[:12]

    cache_path = wrapped_cache_path(
        account_dir=account_dir,
        scope=scope,
        year=year,
        implemented_upto=_IMPLEMENTED_UPTO_ID,
        options_tag=f'v{_CACHE_VERSION}_session_duo',
    )
    if (not refresh) and cache_path.exists():
        try:
            cached_obj = json.loads(cache_path.read_text(encoding='utf-8'))
            if isinstance(cached_obj, dict) and isinstance(cached_obj.get('cards'), list):
                cached_obj['cached'] = True
                cached_obj['availableYears'] = available_years
                return cached_obj
        except Exception:
            pass

    start_ts = int(_dt(year, 1, 1, tzinfo=TZ).timestamp())
    end_ts = int(_dt(year + 1, 1, 1, tzinfo=TZ).timestamp()) - 1

    from chat_export import collect_messages
    messages = collect_messages(
        chat_info, start_ts=start_ts, end_ts=end_ts, keyword=None)

    is_group = bool(uname.endswith('@chatroom') or chat_info.get('is_group'))
    gap_cap = 24 * 3600  # 超过 24h 不算「回复」

    hourly = Counter()
    dow = Counter()
    type_counter = Counter()
    me_type_counter: Counter = Counter()
    peer_type_counter: Counter = Counter()
    member_counter: Counter = Counter()
    member_media: Counter = Counter()
    active_days = set()
    texts = []
    me_count = 0
    peer_count = 0
    me_chars = 0
    peer_chars = 0
    media = 0
    stickers = 0
    voice = 0
    matrix = [[0] * 24 for _ in range(7)]

    # Bidirectional reply gaps (private chats)
    me_reply_gaps: list[int] = []
    peer_reply_gaps: list[int] = []
    prev_is_me: bool | None = None
    prev_ts = 0

    for m in messages:
        if m.get('is_system'):
            continue
        ts = int(m.get('create_time') or 0)
        if not ts:
            continue
        try:
            dt = _dt.fromtimestamp(ts, tz=TZ)
        except (OSError, ValueError):
            continue
        hourly[dt.hour] += 1
        dow[dt.weekday()] += 1
        active_days.add(dt.date().isoformat())
        matrix[dt.weekday()][dt.hour] += 1

        tname = str(m.get('type_name') or m.get('type') or '其他')
        type_counter[tname] += 1
        is_media = tname in ('图片', '视频') or tname in ('3', '43')
        is_sticker = ('表情' in tname) or tname in ('47',)
        is_voice = tname in ('语音', '34') or tname == '语音'
        if is_media:
            media += 1
        if is_sticker:
            stickers += 1
        if is_voice or tname == '语音':
            voice += 1

        is_me = bool(m.get('is_me'))
        content = m.get('text') or ''
        char_n = len(content) if isinstance(content, str) and not content.startswith('[') else 0
        if is_me:
            me_count += 1
            me_chars += char_n
            me_type_counter[tname] += 1
            member_label = '我'
        else:
            peer_count += 1
            peer_chars += char_n
            peer_type_counter[tname] += 1
            member_label = (m.get('sender') or '未知').strip() or '未知'

        if is_group:
            member_counter[member_label] += 1
            if is_media or is_sticker:
                member_media[member_label] += 1

        if isinstance(content, str) and content.strip() and not content.startswith('['):
            texts.append(content.strip())

        if not is_group and prev_is_me is not None and is_me != prev_is_me:
            gap = int(ts) - int(prev_ts)
            if 0 < gap < gap_cap:
                if is_me:
                    me_reply_gaps.append(gap)
                else:
                    peer_reply_gaps.append(gap)
        prev_is_me = is_me
        prev_ts = ts

    total = me_count + peer_count
    peak_hour = hourly.most_common(1)[0][0] if hourly else None
    peak_dow = dow.most_common(1)[0][0] if dow else None
    peak_dow_name = DOW_NAMES[peak_dow] if peak_dow is not None and 0 <= peak_dow < len(DOW_NAMES) else None

    def _avg(xs: list[int]) -> float | None:
        return (sum(xs) / len(xs)) if xs else None

    def _median(xs: list[int]) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        mid = len(s) // 2
        return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0

    # Keywords
    keywords = []
    top_keyword = None
    try:
        import jieba
        from stopwords import is_meaningful, filter_words
        word_counter = Counter()
        for text in texts:
            for word in jieba.cut(text):
                word = word.strip()
                keep, weight = is_meaningful(word)
                if keep:
                    word_counter[word] += weight
        filtered = filter_words(word_counter, total or 1)[:80]
        if filtered:
            max_c = filtered[0][1] or 1
            keywords = [
                {'word': w, 'count': int(c), 'weight': float(c) / float(max_c)}
                for w, c in filtered
            ]
            top_keyword = {'word': filtered[0][0], 'count': int(filtered[0][1])}
    except Exception:
        logger.exception('session wrapped keywords failed')

    def _top_types(counter: Counter, limit: int = 8) -> list[dict[str, Any]]:
        return [{'name': n, 'count': int(c)} for n, c in counter.most_common(limit)]

    if is_group:
        overview_title = '%s · %d 群聊画像' % (display, year)
        overview_narrative = '本报告统计「%s」在 %d 年的成员发言、活跃时段与媒体内容。' % (display, year)
    else:
        overview_title = '与 %s 的 %d 双人报告' % (display, year)
        overview_narrative = '只看你和「%s」这一年的互动：谁说得多、谁回得快、聊什么。' % display

    cards: list[dict[str, Any]] = [
        {
            'id': 0,
            'kind': 'global/overview',
            'title': overview_title,
            'narrative': overview_narrative,
            'data': {
                'totalMessages': total,
                'activeDays': len(active_days),
                'messagesPerDay': (total / max(len(active_days), 1)),
                'sentMediaCount': media,
                'sentStickerCount': stickers,
                'mostActiveHour': peak_hour,
                'mostActiveWeekdayName': peak_dow_name,
                'addedFriends': 0,
                'chatName': display,
                'chatUsername': uname,
                'isDuo': not is_group,
                'meMessages': me_count,
                'peerMessages': peer_count,
            },
        },
    ]

    if not is_group:
        me_share = (me_count / total) if total else 0.0
        cards.append({
            'id': 10,
            'kind': 'duo/balance',
            'title': '谁说得多？',
            'narrative': '双方消息条数与字数对比（系统消息已排除）。',
            'data': {
                'peerName': display,
                'meMessages': me_count,
                'peerMessages': peer_count,
                'meChars': me_chars,
                'peerChars': peer_chars,
                'meShare': round(me_share, 4),
                'peerShare': round(1.0 - me_share, 4) if total else 0.0,
                'totalMessages': total,
            },
        })
        cards.append({
            'id': 11,
            'kind': 'duo/reply_speed',
            'title': '谁回得更快？',
            'narrative': '对方发言后你回复、以及你发言后对方回复的间隔（间隔超过 24 小时不计）。',
            'data': {
                'peerName': display,
                'gapCapSeconds': gap_cap,
                'me': {
                    'replies': len(me_reply_gaps),
                    'avgSeconds': _avg(me_reply_gaps),
                    'medianSeconds': _median(me_reply_gaps),
                    'minSeconds': min(me_reply_gaps) if me_reply_gaps else None,
                    'maxSeconds': max(me_reply_gaps) if me_reply_gaps else None,
                },
                'peer': {
                    'replies': len(peer_reply_gaps),
                    'avgSeconds': _avg(peer_reply_gaps),
                    'medianSeconds': _median(peer_reply_gaps),
                    'minSeconds': min(peer_reply_gaps) if peer_reply_gaps else None,
                    'maxSeconds': max(peer_reply_gaps) if peer_reply_gaps else None,
                },
            },
        })
        cards.append({
            'id': 12,
            'kind': 'duo/types',
            'title': '聊什么？',
            'narrative': '消息类型占比：你发的 vs 对方发的。',
            'data': {
                'peerName': display,
                'meTypes': _top_types(me_type_counter),
                'peerTypes': _top_types(peer_type_counter),
                'allTypes': _top_types(type_counter),
            },
        })
    else:
        members_ranked = [
            {
                'name': name,
                'count': int(cnt),
                'share': round(cnt / total, 4) if total else 0.0,
                'mediaCount': int(member_media.get(name, 0)),
            }
            for name, cnt in member_counter.most_common(40)
        ]
        cards.append({
            'id': 20,
            'kind': 'group/members',
            'title': '发言排行',
            'narrative': '按消息条数统计群成员活跃度（系统消息已排除）。',
            'data': {
                'groupName': display,
                'members': members_ranked,
                'uniqueSpeakers': len(member_counter),
                'totalMessages': total,
            },
        })
        cards.append({
            'id': 21,
            'kind': 'group/media',
            'title': '媒体与内容',
            'narrative': '图片/视频、表情、语音等占比，以及发媒体最多的成员。',
            'data': {
                'groupName': display,
                'totalMessages': total,
                'mediaCount': media,
                'stickerCount': stickers,
                'voiceCount': voice,
                'mediaShare': round(media / total, 4) if total else 0.0,
                'types': _top_types(type_counter),
                'topMediaMembers': [
                    {'name': n, 'count': int(c)}
                    for n, c in member_media.most_common(10)
                ],
            },
        })

    cards.append({
        'id': 1,
        'kind': 'time/weekday_hour_heatmap',
        'title': '活跃时段',
        'narrative': '这个群最常在什么时候热闹？' if is_group else '你们最常在什么时候聊天？',
        'data': {
            'weekdayLabels': list(DOW_NAMES),
            'hourLabels': ['%02d' % h for h in range(24)],
            'matrix': matrix,
            'totalMessages': total,
        },
    })
    cards.append({
        'id': 5,
        'kind': 'text/keywords_wordcloud',
        'title': '会话关键词',
        'narrative': '从这一年的正文里抽出的高频词。',
        'data': {
            'keywords': keywords,
            'topKeyword': top_keyword,
            'examples': [],
        },
    })

    obj: dict[str, Any] = {
        'account': account_dir.name,
        'year': year,
        'year_adjusted': year != requested_year,
        'requested_year': requested_year,
        'scope': scope,
        'chat': uname,
        'chatDisplayName': display,
        'isDuo': not is_group,
        'isGroup': is_group,
        'username': get_account_wxid(account_dir),
        'generated_at': int(time.time()),
        'cached': False,
        'availableYears': available_years,
        'cards': cards,
    }
    try:
        cache_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        logger.exception('Failed to write session wrapped cache: %s', cache_path)
    return obj
