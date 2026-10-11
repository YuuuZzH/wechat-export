"""
词云分析 — jieba 分词 + 智能停用词 + ECharts 交互式 HTML 输出。
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime

from engine.constants import TZ
from engine.services.emoji_map import WECHAT_EMOJI_MAP
from stopwords import is_meaningful, filter_words

try:
    import zstandard as _zstd_mod
except ImportError:
    _zstd_mod = None

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _decode_msg_text(message_content, compress_content=None):
    """Decode WCDB message content, handling zstd compression and hex/base64 encoding."""
    # Normalize to bytes
    if isinstance(message_content, bytes):
        raw = message_content
    elif isinstance(message_content, str):
        # Try hex decode first
        s = message_content.strip()
        if len(s) >= 16 and len(s) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", s):
            try:
                raw = bytes.fromhex(s)
            except Exception:
                raw = s.encode("utf-8", errors="replace")
        else:
            raw = s.encode("utf-8", errors="replace")
    else:
        return ""

    # zstd decompression
    if _zstd_mod is not None and len(raw) >= 4 and raw[:4] == _ZSTD_MAGIC:
        try:
            out = _zstd_mod.decompress(raw)
            return out.decode("utf-8", errors="replace")
        except Exception:
            pass

    # Plain UTF-8
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


# Compiled regex for WeChat emoji [xxx] codes — sorted longest-first so
# longer codes match before shorter ones when embedded in alternation.
_EMOJI_CODES_RE = re.compile(
    '|'.join(re.escape(code) for code in sorted(WECHAT_EMOJI_MAP.keys(), key=len, reverse=True))
)


def _clean_for_tokenize(text: str) -> str:
    """Strip WeChat emoji [xxx] codes, XML tags, control chars; normalize whitespace."""
    s = str(text or "").strip()
    if not s:
        return ""
    s = _EMOJI_CODES_RE.sub("", s)
    s = re.sub(r'<[^>]+>', '', s)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def extract_text_messages(decrypted_dir, chat_info=None, start_ts=None, end_ts=None):
    """提取文本消息用于分词。
    Args:
        decrypted_dir: 解密后的数据目录
        chat_info: 可选，指定某个 chat 的 info dict（包含 username 和 tables）
        start_ts: 可选，起始时间戳(秒)
        end_ts: 可选，结束时间戳(秒)
    Returns: (所有文本内容字符串, 总消息数)
    """
    if chat_info:
        tables = chat_info["tables"]
    else:
        # Global: iterate all message DBs.
        # Try the message/ subdirectory first (old decrypted layout), then fall
        # back to scanning the root directory for .db files (flat backup layout).
        tables = []
        msg_dir = os.path.join(decrypted_dir, "message")
        db_paths: list[tuple[int, str]] = []

        if os.path.isdir(msg_dir):
            for f in os.listdir(msg_dir):
                m = re.match(r'message_(\d+)\.db', f)
                if m:
                    db_paths.append((int(m.group(1)), os.path.join(msg_dir, f)))

        if not db_paths:
            # Flat backup layout: .db files directly in decrypted_dir
            for f in os.listdir(decrypted_dir):
                if re.match(r'(message|biz_message)(_\d+)?\.db', f, re.IGNORECASE):
                    db_paths.append((0, os.path.join(decrypted_dir, f)))

        db_paths.sort(key=lambda x: x[0])
        for idx, db_path in db_paths:
            tables.append({"db_idx": idx, "db_path": db_path, "table_name": None})

    # Build time filter clause with parameterized placeholders
    time_clause = ""
    time_params = ()
    if start_ts is not None:
        time_clause += " AND create_time >= ?"
        time_params += (start_ts,)
    if end_ts is not None:
        time_clause += " AND create_time <= ?"
        time_params += (end_ts,)

    all_texts = []
    total_msgs = 0

    for tinfo in tables:
        db_path = tinfo["db_path"]
        if not os.path.exists(db_path):
            continue
        conn = sqlite3.connect(db_path)
        try:
            if tinfo["table_name"]:
                # Specific chat table
                query = f"""
                    SELECT message_content, compress_content FROM [{tinfo['table_name']}]
                    WHERE local_type = 1 AND (message_content IS NOT NULL OR compress_content IS NOT NULL)
                    AND create_time > 1000000000{time_clause}
                """
            else:
                # All Msg_ tables in this DB
                try:
                    msgtables = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    ).fetchall()
                except Exception:
                    msgtables = []

                if not msgtables:
                    conn.close()
                    continue

                # Batch UNION ALL to stay under SQLite's 500-term compound SELECT limit
                _batch = []
                _batch_size = 200
                for i in range(0, len(msgtables), _batch_size):
                    chunk = msgtables[i:i + _batch_size]
                    query = " UNION ALL ".join(
                        f"SELECT message_content, compress_content FROM [{t[0]}] WHERE local_type = 1 "
                        f"AND (message_content IS NOT NULL OR compress_content IS NOT NULL)"
                        f" AND create_time > 1000000000{time_clause}"
                        for t in chunk
                    )
                    batch_params = time_params * len(chunk)
                    try:
                        for row in conn.execute(query, batch_params):
                            _batch.append(row)
                    except Exception:
                        pass
                results = _batch

            if tinfo.get("table_name"):
                results = conn.execute(query, time_params).fetchall()

            for row in results:
                mc = row[0] if len(row) > 0 else None
                cc = row[1] if len(row) > 1 else None
                text = _decode_msg_text(mc, cc)
                if not text:
                    continue
                # Strip group chat sender prefix
                if ":\n" in text[:80]:
                    text = text.split(":\n", 1)[-1]
                text = text.strip()
                if len(text) >= 2:
                    all_texts.append(text)
                    total_msgs += 1
            conn.close()
        except Exception:
            conn.close()

    return all_texts, total_msgs


def generate_wordcloud(decrypted_dir, chat_info=None, out_path=None, max_words=200,
                       start_ts=None, end_ts=None,
                       print_fn=None, progress_fn=None):
    """生成词云 HTML。
    Args:
        decrypted_dir: 解密数据目录
        chat_info: 可选，指定聊天的 info dict
        out_path: 输出 HTML 路径
        max_words: 最大词数
        start_ts: 可选，起始时间戳(秒)
        end_ts: 可选，结束时间戳(秒)
    Returns: out_path
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    try:
        import jieba
    except ImportError:
        print_fn("[ERROR] 需要 jieba 库: pip install jieba")
        return None

    progress_fn(10, "提取文本消息...")
    texts, total_msgs = extract_text_messages(decrypted_dir, chat_info, start_ts, end_ts)
    if not texts:
        print_fn("未找到文本消息，无法生成词云")
        return None

    print_fn(f"提取 {len(texts)} 条文本消息")

    # Determine name
    if chat_info:
        name = chat_info.get("display_name", chat_info.get("username", "未命名"))
    else:
        name = "全局词云"

    # Check if jieba supports pos tagging
    progress_fn(30, "分词中...")
    has_pos = False
    try:
        import jieba.posseg as pseg
        has_pos = True
    except ImportError:
        pass

    word_counter = Counter()

    if has_pos:
        for text in texts:
            cleaned = _clean_for_tokenize(text)
            if not cleaned:
                continue
            for word, flag in pseg.cut(cleaned):
                word = word.strip()
                keep, weight = is_meaningful(word, pos=flag)
                if keep:
                    word_counter[word] += 1 * weight
    else:
        for text in texts:
            cleaned = _clean_for_tokenize(text)
            if not cleaned:
                continue
            for word in jieba.cut(cleaned):
                word = word.strip()
                keep, weight = is_meaningful(word)
                if keep:
                    word_counter[word] += 1 * weight

    progress_fn(60, f"分词完成: {len(word_counter)} 个有效词")

    # Apply context frequency filter
    results = filter_words(word_counter, total_msgs)
    top_words = results[:max_words]

    if not top_words:
        print_fn("过滤后无有效词汇")
        return None

    print_fn(f"去停用词后: {len(results)} 个词, 展示 Top {len(top_words)}")

    progress_fn(75, "生成词云 HTML...")

    # Determine output path
    if out_path is None:
        safe = re.sub(r'[<>:"/\\|?*]', '_', name)[:30]
        out_path = os.path.join(decrypted_dir, "..", "..", "output", "wordcloud",
                                f"{safe}.html")
        out_path = os.path.normpath(out_path)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Generate HTML（脚本拷到同目录，离线可打开）
    html = _build_wordcloud_html(
        name, top_words, total_msgs, len(results), out_path=out_path)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)

    progress_fn(98, f"词云已生成: {out_path}")
    print_fn(f"词云已生成: {out_path}")
    return out_path


def _build_wordcloud_html(title, top_words, total_msgs, unique_words, out_path=None):
    """构建 ECharts 词云 HTML。"""
    # Prepare word cloud data: [{name, value}, ...]
    max_count = top_words[0][1] if top_words else 1
    word_data = []
    for word, count in top_words:
        word_data.append({"name": word, "value": round(count, 1)})

    # Top 50 bar chart data (reversed for horizontal bar)
    bar_data = top_words[:50]
    bar_labels = [w for w, c in bar_data][::-1]
    bar_values = [round(c, 1) for w, c in bar_data][::-1]

    word_data_json = json.dumps(word_data, ensure_ascii=False)
    bar_labels_json = json.dumps(bar_labels, ensure_ascii=False)
    bar_values_json = json.dumps(bar_values)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    from offline_assets import echarts_tags
    echarts_script = echarts_tags(out_path, wordcloud=True)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>词云 - {title}</title>
{echarts_script}
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: "Microsoft YaHei", sans-serif; background: #1a1a2e; color: #eee; }}
.header {{ text-align: center; padding: 24px; background: linear-gradient(135deg, #16213e, #0f3460); }}
.header h1 {{ font-size: 24px; margin-bottom: 8px; }}
.header .stats {{ color: #aaa; font-size: 13px; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 16px; }}
.charts {{ display: flex; flex-wrap: wrap; gap: 16px; }}
.chart-box {{ background: #16213e; border-radius: 12px; padding: 16px; flex: 1 1 600px; min-width: 400px; }}
.chart-box h2 {{ font-size: 16px; margin-bottom: 12px; color: #e94560; }}
.chart {{ width: 100%; height: 500px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
th, td {{ padding: 6px 12px; text-align: left; font-size: 13px; border-bottom: 1px solid #2a2a4a; }}
th {{ color: #e94560; }}
tr:hover {{ background: rgba(233,69,96,0.1); }}
.num {{ text-align: right; color: #aaa; }}
</style>
</head>
<body>
<div class="header">
  <h1>词云分析: {title}</h1>
  <div class="stats">总消息: {total_msgs:,} · 有效词汇: {unique_words:,} · 展示: {len(top_words)} · 生成: {ts}</div>
</div>
<div class="container">
  <div class="charts">
    <div class="chart-box">
      <h2>词云图</h2>
      <div id="wordcloud" class="chart"></div>
    </div>
    <div class="chart-box">
      <h2>Top 50 高频词</h2>
      <div id="barchart" class="chart"></div>
    </div>
  </div>
  <div class="chart-box" style="margin-top:16px">
    <h2>完整词频表 (Top 200)</h2>
    <table>
      <tr><th>#</th><th>词汇</th><th class="num">加权词频</th></tr>
'''
    for i, (word, count) in enumerate(top_words[:200], 1):
        html += f'<tr><td>{i}</td><td>{word}</td><td class="num">{count:.1f}</td></tr>\n'

    html += f'''</table>
  </div>
</div>
<script>
(function() {{
  var wordData = {word_data_json};
  var barLabels = {bar_labels_json};
  var barValues = {bar_values_json};

  // Word cloud
  var wc = echarts.init(document.getElementById('wordcloud'));
  wc.setOption({{
    backgroundColor: 'transparent',
    tooltip: {{ show: true }},
    series: [{{
      type: 'wordCloud',
      shape: 'circle',
      left: 'center',
      top: 'center',
      width: '90%',
      height: '90%',
      sizeRange: [14, 80],
      rotationRange: [-45, 45],
      rotationStep: 15,
      gridSize: 8,
      drawOutOfBound: false,
      textStyle: {{
        fontFamily: 'Microsoft YaHei, sans-serif',
        fontWeight: 'normal',
        color: function() {{
          var colors = ['#e94560','#0f3460','#533483','#16a085','#f39c12',
                        '#2980b9','#8e44ad','#2c3e50','#d35400','#c0392b',
                        '#1abc9c','#3498db','#9b59b6','#e67e22','#e74c3c'];
          return colors[Math.floor(Math.random() * colors.length)];
        }}
      }},
      emphasis: {{ textStyle: {{ fontSize: 24, fontWeight: 'bold' }} }},
      data: wordData
    }}]
  }});

  // Bar chart
  var bc = echarts.init(document.getElementById('barchart'));
  bc.setOption({{
    backgroundColor: 'transparent',
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    grid: {{ left: 120, right: 30, top: 10, bottom: 10 }},
    xAxis: {{ type: 'value', axisLabel: {{ color: '#aaa' }}, splitLine: {{ lineStyle: {{ color: '#2a2a4a' }} }} }},
    yAxis: {{ type: 'category', data: barLabels, axisLabel: {{ color: '#ccc', fontSize: 12 }}, inverse: true }},
    series: [{{
      type: 'bar',
      data: barValues,
      itemStyle: {{ color: '#e94560', borderRadius: [0, 4, 4, 0] }},
      barMaxWidth: 20,
    }}]
  }});

  window.addEventListener('resize', function() {{ wc.resize(); bc.resize(); }});
}})();
</script>
</body>
</html>'''
    return html
