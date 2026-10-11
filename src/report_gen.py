"""
HTML 综合报告 — 概览卡片 + 消息类型饼图 + 年度趋势 + 24h 热力图。
ECharts 本地 vendor 离线加载，多 Tab 懒渲染。
"""
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

from engine.constants import TZ, MSG_TYPES_CN, MSG_TYPE_ORDER, MSG_TYPE_COLORS, DOW_NAMES


def collect_stats(decrypted_dir, start_ts=None, end_ts=None,
                   print_fn=None, progress_fn=None):
    """收集所有统计数据。
    Args:
        start_ts: 可选，起始时间戳(秒)
        end_ts: 可选，结束时间戳(秒)
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    msg_dir = os.path.join(decrypted_dir, "message")
    msg_dbs = []
    if os.path.isdir(msg_dir):
        for f in os.listdir(msg_dir):
            m = re.match(r'message_(\d+)\.db', f)
            if m:
                msg_dbs.append((int(m.group(1)), os.path.join(msg_dir, f)))
    if not msg_dbs:
        # Flat backup layout: .db files directly in decrypted_dir
        try:
            for f in sorted(os.listdir(decrypted_dir)):
                if re.match(r'message_\d+\.db', f, re.IGNORECASE):
                    msg_dbs.append((0, os.path.join(decrypted_dir, f)))
        except OSError:
            pass
    msg_dbs.sort(key=lambda x: x[0])

    # Build hash -> username map
    hash_to_name = {}
    for idx, db_path in msg_dbs:
        if not os.path.exists(db_path):
            continue
        conn = sqlite3.connect(db_path)
        try:
            for (uname,) in conn.execute("SELECT user_name FROM Name2Id"):
                if uname:
                    hash_to_name[hashlib.md5(uname.encode()).hexdigest()] = uname
        except Exception:
            pass
        finally:
            conn.close()

    total_msgs = 0
    type_counter = Counter()
    hourly = Counter()
    dow_counter = Counter()
    yearly_counter = Counter()
    monthly_counter = Counter()
    chat_counter = Counter()
    is_group_cache = {}

    total_tables = 0
    processed = 0

    # Count total tables first
    for idx, db_path in msg_dbs:
        if not os.path.exists(db_path):
            continue
        conn = sqlite3.connect(db_path)
        try:
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchone()[0]
            total_tables += tables
        except Exception:
            pass
        finally:
            conn.close()

    for idx, db_path in msg_dbs:
        if not os.path.exists(db_path):
            continue
        progress_fn(10 + processed * 70 // max(total_tables, 1),
                    f"分析消息 DB {idx}...")
        conn = sqlite3.connect(db_path)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (tname,) in tables:
                h = tname[4:]
                uname = hash_to_name.get(h, h[:8])

                # Build time filter
                time_conds = ["create_time > 1000000000"]
                if start_ts is not None:
                    time_conds.append(f"create_time >= {start_ts}")
                if end_ts is not None:
                    time_conds.append(f"create_time <= {end_ts}")
                time_clause = " AND ".join(time_conds)

                try:
                    for r in conn.execute(
                        f"SELECT local_type, create_time FROM [{tname}] "
                        f"WHERE {time_clause}"
                    ):
                        ltype = r[0] & 0xFFFFFFFF if isinstance(r[0], int) else r[0]
                        ts = r[1]
                        dt = datetime.fromtimestamp(ts, tz=TZ)

                        type_cn = MSG_TYPES_CN.get(ltype, f"未知({ltype})")
                        type_counter[type_cn] += 1
                        hourly[dt.hour] += 1
                        dow_counter[dt.weekday()] += 1
                        yearly_counter[dt.year] += 1
                        monthly_counter[f"{dt.year}-{dt.month:02d}"] += 1
                        chat_counter[uname] += 1
                        total_msgs += 1
                except Exception:
                    pass
                processed += 1
        except Exception:
            pass
        finally:
            conn.close()

    progress_fn(85, "统计数据收集完成")

    # Resolve chat names
    chat_list = []
    contact_db = os.path.join(decrypted_dir, "contact", "contact.db")
    if not os.path.exists(contact_db):
        # Fallback: output/decrypted for backup-based data
        alt = os.path.normpath(os.path.join(decrypted_dir, "..", "..", "..",
                                            "output", "decrypted", "contact", "contact.db"))
        if os.path.isfile(alt):
            contact_db = alt
    id_to_name = {}
    name_to_id = {}
    if os.path.exists(contact_db):
        cconn = sqlite3.connect(contact_db)
        try:
            for r in cconn.execute(
                "SELECT id, username, COALESCE(remark, nick_name, alias, username) FROM contact"
            ):
                if r[0] and r[1] and r[2]:
                    id_to_name[r[0]] = r[2].strip()
                    name_to_id[r[1]] = r[0]
        finally:
            cconn.close()

    for uname, count in chat_counter.most_common(50):
        display = uname
        if uname in name_to_id and name_to_id[uname] in id_to_name:
            display = id_to_name[name_to_id[uname]]
        elif uname.endswith("@chatroom"):
            short = uname[:12] + "..." if len(uname) > 12 else uname
            display = f"群聊({short})"
        chat_list.append({"name": display, "username": uname, "count": count})

    return {
        "total_msgs": total_msgs,
        "total_chats": len(chat_counter),
        "type_counter": type_counter,
        "hourly": hourly,
        "dow": dow_counter,
        "yearly": yearly_counter,
        "monthly": monthly_counter,
        "top_chats": chat_list[:20],
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def generate_report(decrypted_dir, out_path=None, start_ts=None, end_ts=None,
                     print_fn=None, progress_fn=None):
    """生成综合 HTML 报告。
    Args:
        start_ts: 可选，起始时间戳(秒)
        end_ts: 可选，结束时间戳(秒)
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    progress_fn(5, "开始收集统计数据...")
    stats = collect_stats(decrypted_dir, start_ts, end_ts, print_fn, progress_fn)

    if stats["total_msgs"] == 0:
        print_fn("未找到任何消息，无法生成报告")
        return None

    print_fn(f"分析 {stats['total_msgs']:,} 条消息, {stats['total_chats']} 个会话")

    progress_fn(90, "生成 HTML 报告...")

    if out_path is None:
        out_path = os.path.join(decrypted_dir, "..", "..", "output", "report.html")
        out_path = os.path.normpath(out_path)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    html = _build_report_html(stats, out_path=out_path)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)

    progress_fn(98, f"报告已生成: {out_path}")
    print_fn(f"报告已生成: {out_path}")
    return out_path


def _build_report_html(stats, out_path=None):
    """构建 ECharts 多 Tab 报告 HTML。"""
    s = stats

    # Prepare chart data as JSON
    type_data = []
    for tname in MSG_TYPE_ORDER:
        if tname in s["type_counter"]:
            type_data.append({"name": tname, "value": s["type_counter"][tname]})
    type_data_json = json.dumps(type_data, ensure_ascii=False)

    # Hourly data
    hours = list(range(24))
    hour_values = [s["hourly"].get(h, 0) for h in hours]
    # Hour labels
    hour_labels = [f"{h:02d}:00" for h in hours]
    max_hour = max(hour_values) if hour_values else 1

    # Heatmap data: [hour, dow, expected_count] — expected frequency under
    # independence assumption, since we don't have cross-tabulated hour×dow data.
    heatmap_data = []
    total = max(s.get("total_msgs", 1), 1)
    for h in range(24):
        for d in range(7):
            v = int(s["hourly"].get(h, 0) * s["dow"].get(d, 0) / total)
            if v > 0:
                heatmap_data.append([h, d, v])

    heatmap_json = json.dumps(heatmap_data)

    # Yearly data
    years = sorted(s["yearly"].keys())
    yearly_values = [s["yearly"][y] for y in years]

    # DOW data
    dow_values = [s["dow"].get(d, 0) for d in range(7)]

    # Pre-compute peak hour/day for cards (can't inline :02d in f-string conditional)
    if s['hourly']:
        peak_hour = s['hourly'].most_common(1)[0][0]
        peak_hour_str = f'{peak_hour:02d}:00'
    else:
        peak_hour_str = 'N/A'
    if s['dow']:
        peak_dow_str = DOW_NAMES[max(s['dow'], key=s['dow'].get)]
    else:
        peak_dow_str = 'N/A'

    # Monthly data
    months_sorted = sorted(s["monthly"].keys())[-24:]  # last 24 months
    monthly_values = [s["monthly"][m] for m in months_sorted]

    # Top chats
    chat_names = [c["name"][:20] for c in s["top_chats"][:15]][::-1]
    chat_counts = [c["count"] for c in s["top_chats"][:15]][::-1]

    # Colors for types
    colors_json = json.dumps(MSG_TYPE_COLORS, ensure_ascii=False)

    from offline_assets import echarts_tags
    echarts_script = echarts_tags(out_path, wordcloud=False)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WeChat Export · 聊天分析报告</title>
{echarts_script}
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:"Microsoft YaHei",sans-serif; background:#0d1117; color:#c9d1d9; }}
.header {{ text-align:center; padding:32px 16px; background:linear-gradient(135deg,#0d1117,#161b22); border-bottom:1px solid #30363d; }}
.header h1 {{ font-size:26px; color:#58a6ff; margin-bottom:8px; }}
.header .sub {{ color:#8b949e; font-size:13px; }}
.toolbar {{ display:flex; justify-content:flex-end; max-width:1400px; margin:12px auto 0; padding:0 20px; }}
.toolbar button {{ padding:6px 16px; background:#1c2333; border:1px solid #30363d; border-radius:6px; color:#8b949e; cursor:pointer; font-size:12px; font-family:inherit; }}
.toolbar button.active {{ background:#1f3a5f; color:#58a6ff; border-color:#58a6ff; }}
.toolbar button:hover {{ color:#c9d1d9; }}
.cards {{ display:flex; flex-wrap:wrap; gap:16px; padding:20px; max-width:1400px; margin:0 auto; }}
.card {{ flex:1 1 200px; min-width:160px; background:#161b22; border:1px solid #30363d; border-radius:12px; padding:20px; text-align:center; }}
.card .num {{ font-size:32px; font-weight:bold; color:#58a6ff; }}
.card .label {{ font-size:13px; color:#8b949e; margin-top:4px; }}
.tabs {{ display:flex; gap:4px; padding:0 20px; max-width:1400px; margin:0 auto; }}
.tab {{ padding:8px 20px; background:#161b22; border:1px solid #30363d; border-bottom:none; border-radius:8px 8px 0 0; color:#8b949e; cursor:pointer; font-size:13px; }}
.tab.active {{ background:#1c2333; color:#58a6ff; border-color:#30363d #30363d #1c2333; }}
.tab-content {{ display:none; max-width:1400px; margin:0 auto; padding:16px 20px; }}
.tab-content.active {{ display:block; }}
.chart-row {{ display:flex; flex-wrap:wrap; gap:16px; }}
.chart-panel {{ flex:1 1 500px; min-width:350px; background:#161b22; border:1px solid #30363d; border-radius:12px; padding:16px; }}
.chart-panel.full {{ flex:1 1 100%; }}
.chart-panel h3 {{ font-size:15px; color:#e94560; margin-bottom:12px; }}
.chart {{ width:100%; height:380px; }}
.chart.tall {{ height:500px; }}
.footer {{ text-align:center; padding:20px; color:#484f58; font-size:12px; }}
</style>
</head>
<body>
<div class="header">
  <h1>WeChat Export · 聊天分析报告</h1>
  <div class="sub">生成时间: {s['generated_at']} · 分析 {s['total_msgs']:,} 条消息 · {s['total_chats']} 个会话</div>
</div>

<div class="toolbar">
  <button id="btn-toggle-labels" class="active" onclick="toggleLabels()">隐藏数据标签</button>
</div>

<div class="cards">
  <div class="card"><div class="num">{s['total_msgs']:,}</div><div class="label">总消息数</div></div>
  <div class="card"><div class="num">{s['total_chats']:,}</div><div class="label">会话数</div></div>
  <div class="card"><div class="num">{len(s['type_counter'])}</div><div class="label">消息类型</div></div>
  <div class="card"><div class="num">{len(years)}</div><div class="label">年份跨度</div></div>
  <div class="card"><div class="num">{peak_hour_str}</div><div class="label">最活跃时段</div></div>
  <div class="card"><div class="num">{peak_dow_str}</div><div class="label">最活跃星期</div></div>
</div>

<div class="tabs">
  <div class="tab active" data-tab="overview" onclick="switchTab('overview')">概览</div>
  <div class="tab" data-tab="time" onclick="switchTab('time')">时间分析</div>
  <div class="tab" data-tab="chats" onclick="switchTab('chats')">热门会话</div>
</div>

<div id="tab-overview" class="tab-content active">
  <div class="chart-row">
    <div class="chart-panel">
      <h3>消息类型分布</h3>
      <div id="chart-type-pie" class="chart"></div>
    </div>
    <div class="chart-panel">
      <h3>年度趋势</h3>
      <div id="chart-yearly" class="chart"></div>
    </div>
  </div>
</div>

<div id="tab-time" class="tab-content">
  <div class="chart-row">
    <div class="chart-panel">
      <h3>24小时分布</h3>
      <div id="chart-hourly" class="chart"></div>
    </div>
    <div class="chart-panel">
      <h3>星期分布</h3>
      <div id="chart-dow" class="chart"></div>
    </div>
  </div>
  <div class="chart-row" style="margin-top:16px">
    <div class="chart-panel full">
      <h3>24h x 7天 活跃热力图</h3>
      <div id="chart-heatmap" class="chart tall"></div>
    </div>
  </div>
  <div class="chart-row" style="margin-top:16px">
    <div class="chart-panel full">
      <h3>月度趋势 (近24个月)</h3>
      <div id="chart-monthly" class="chart tall"></div>
    </div>
  </div>
</div>

<div id="tab-chats" class="tab-content">
  <div class="chart-row">
    <div class="chart-panel full">
      <h3>Top 15 活跃会话</h3>
      <div id="chart-chats" class="chart tall"></div>
    </div>
  </div>
</div>

<div class="footer">WeChat Export · 数据来源: 本地解密数据库</div>

<script>
var typeColors = {colors_json};
var typeData = {type_data_json};

function switchTab(name) {{
  document.querySelectorAll('.tab').forEach(function(t) {{ t.classList.remove('active'); }});
  document.querySelectorAll('.tab-content').forEach(function(c) {{ c.classList.remove('active'); }});
  var tab = document.querySelector('.tab[data-tab="' + name + '"]');
  if (tab) tab.classList.add('active');
  var content = document.getElementById('tab-' + name);
  if (content) content.classList.add('active');
  setTimeout(resizeAll, 100);
}}

var charts = {{}};
function getChart(id) {{
  if (!charts[id]) {{
    var el = document.getElementById(id);
    if (el) charts[id] = echarts.init(el);
  }}
  return charts[id];
}}

function resizeAll() {{
  Object.values(charts).forEach(function(c) {{ try {{ c.resize(); }} catch(e) {{}} }});
}}

// Type Pie
(function() {{
  var c = getChart('chart-type-pie');
  if (!c) return;
  c.setOption({{
    backgroundColor:'transparent',
    tooltip:{{trigger:'item',formatter:'{{b}}: {{c}} ({{d}}%)'}},
    legend:{{orient:'vertical',right:10,top:'center',textStyle:{{color:'#8b949e',fontSize:11}}}},
    series:[{{
      type:'pie',radius:['40%','75%'],center:['40%','50%'],
      label:{{show:true,formatter:'{{b}}\\n{{d}}%',fontSize:10,color:'#8b949e'}},
      emphasis:{{label:{{show:true,fontSize:14,fontWeight:'bold'}}}},
      data:typeData,
      itemStyle:{{color:function(p){{return typeColors[p.name]||'#666';}}}}
    }}]
  }});
}})();

// Yearly Bar
(function() {{
  var c = getChart('chart-yearly');
  if (!c) return;
  c.setOption({{
    backgroundColor:'transparent',
    tooltip:{{trigger:'axis'}},
    grid:{{left:50,right:20,top:20,bottom:30}},
    xAxis:{{type:'category',data:{json.dumps(years)},axisLabel:{{color:'#8b949e'}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b949e'}},splitLine:{{lineStyle:{{color:'#21262d'}}}}}},
    series:[{{
      type:'bar',data:{json.dumps(yearly_values)},
      label:{{show:true,position:'top',fontSize:10,color:'#8b949e'}},
      itemStyle:{{color:'#58a6ff',borderRadius:[4,4,0,0]}},
      barMaxWidth:40
    }}]
  }});
}})();

// Hourly Line
(function() {{
  var c = getChart('chart-hourly');
  if (!c) return;
  var peakHour = {max(s['hourly'], key=s['hourly'].get) if s['hourly'] else 0};
  c.setOption({{
    backgroundColor:'transparent',
    tooltip:{{trigger:'axis'}},
    grid:{{left:50,right:20,top:20,bottom:30}},
    xAxis:{{type:'category',data:{json.dumps(hour_labels)},axisLabel:{{color:'#8b949e'}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b949e'}},splitLine:{{lineStyle:{{color:'#21262d'}}}}}},
    series:[{{
      type:'line',data:{json.dumps(hour_values)},
      smooth:true,symbol:'circle',symbolSize:6,
      lineStyle:{{color:'#58a6ff',width:2}},
      itemStyle:{{color:'#58a6ff'}},
      areaStyle:{{color:{{type:'linear',x:0,y:0,x2:0,y2:1,
        colorStops:[{{offset:0,color:'rgba(88,166,255,0.3)'}},{{offset:1,color:'rgba(88,166,255,0)'}}]}}}},
      markPoint:{{data:[{{name:'峰值',coord:[peakHour,{max_hour}]}}],
        symbol:'pin',symbolSize:40,itemStyle:{{color:'#e94560'}}}}
    }}]
  }});
}})();

// DOW Bar
(function() {{
  var c = getChart('chart-dow');
  if (!c) return;
  c.setOption({{
    backgroundColor:'transparent',
    tooltip:{{trigger:'axis'}},
    grid:{{left:50,right:20,top:20,bottom:30}},
    xAxis:{{type:'category',data:{json.dumps(DOW_NAMES)},axisLabel:{{color:'#8b949e'}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b949e'}},splitLine:{{lineStyle:{{color:'#21262d'}}}}}},
    series:[{{
      type:'bar',data:{json.dumps(dow_values)},
      label:{{show:true,position:'top',fontSize:10,color:'#8b949e'}},
      itemStyle:{{color:function(p){{return p.dataIndex>=5?'#e94560':'#58a6ff';}},borderRadius:[4,4,0,0]}},
      barMaxWidth:40
    }}]
  }});
}})();

// Heatmap
(function() {{
  var c = getChart('chart-heatmap');
  if (!c) return;
  c.setOption({{
    backgroundColor:'transparent',
    tooltip:{{position:'top'}},
    grid:{{left:80,right:20,top:10,bottom:50}},
    xAxis:{{type:'category',data:[{','.join(f"'{h:02d}:00'" for h in hours)}],axisLabel:{{color:'#8b949e',fontSize:10}}}},
    yAxis:{{type:'category',data:{json.dumps(DOW_NAMES)},axisLabel:{{color:'#8b949e'}}}},
    visualMap:{{min:0,max:{max([d[2] for d in heatmap_data]) if heatmap_data else 1},
      calculable:true,orient:'horizontal',left:'center',bottom:0,
      inRange:{{color:['#0d1117','#0f3460','#533483','#e94560','#f39c12']}},
      textStyle:{{color:'#8b949e'}}
    }},
    series:[{{type:'heatmap',data:{heatmap_json},
      label:{{show:false}},emphasis:{{itemStyle:{{shadowBlur:10,shadowColor:'rgba(0,0,0,0.5)'}}}}
    }}]
  }});
}})();

// Monthly Line
(function() {{
  var c = getChart('chart-monthly');
  if (!c) return;
  c.setOption({{
    backgroundColor:'transparent',
    tooltip:{{trigger:'axis'}},
    grid:{{left:60,right:20,top:20,bottom:40}},
    xAxis:{{type:'category',data:{json.dumps(months_sorted)},axisLabel:{{color:'#8b949e',rotate:45,fontSize:10}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b949e'}},splitLine:{{lineStyle:{{color:'#21262d'}}}}}},
    series:[{{
      type:'line',data:{json.dumps(monthly_values)},
      smooth:true,lineStyle:{{color:'#e94560',width:2}},
      areaStyle:{{color:{{type:'linear',x:0,y:0,x2:0,y2:1,
        colorStops:[{{offset:0,color:'rgba(233,69,96,0.3)'}},{{offset:1,color:'rgba(233,69,96,0)'}}]}}}}
    }}]
  }});
}})();

// Top Chats
(function() {{
  var c = getChart('chart-chats');
  if (!c) return;
  c.setOption({{
    backgroundColor:'transparent',
    tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}}}},
    grid:{{left:160,right:30,top:10,bottom:20}},
    xAxis:{{type:'value',axisLabel:{{color:'#8b949e'}},splitLine:{{lineStyle:{{color:'#21262d'}}}}}},
    yAxis:{{type:'category',data:{json.dumps(chat_names)},axisLabel:{{color:'#8b949e',fontSize:11,width:140,overflow:'truncate'}},inverse:true}},
    series:[{{
      type:'bar',data:{json.dumps(chat_counts)},
      label:{{show:true,position:'right',fontSize:10,color:'#8b949e'}},
      itemStyle:{{color:'#58a6ff',borderRadius:[0,4,4,0]}},
      barMaxWidth:16
    }}]
  }});
}})();

// Toggle all chart labels
function toggleLabels() {{
  var btn = document.getElementById('btn-toggle-labels');
  var show = !btn.classList.contains('active');
  btn.classList.toggle('active');
  btn.textContent = show ? '隐藏数据标签' : '显示数据标签';

  Object.values(charts).forEach(function(c) {{
    if (!c) return;
    var opt = c.getOption();
    if (!opt || !opt.series) return;
    var changed = false;
    opt.series.forEach(function(s) {{
      if (s.label) {{ s.label.show = show; changed = true; }}
    }});
    if (changed) c.setOption(opt);
  }});
}}

window.addEventListener('resize', resizeAll);
</script>
</body>
</html>'''
    return html
