"""
统计某个群里不同成员的发言占比，可按时间段分桶并生成 HTML 可视化。

用法:
    python3 group_stats.py <群名> [--bucket month|week|year|all]
                          [--top N] [--since YYYY-MM-DD] [--until ...]
                          [--json out.json] [--html out.html]

示例:
    python3 group_stats.py "我们的群"
    python3 group_stats.py "我们的群" --bucket month --html group.html
    python3 group_stats.py "我们的群" --bucket week --top 10 --since 2025-01-01

实现:
    - 群消息表 = Msg_<md5(群 username)>，可能跨多个 message_N.db shard。
    - 每条消息的发送方为 real_sender_id (int)，需经 *该 shard 自身* 的 Name2Id
      映射到群成员的 username，再通过 contact.db 还原显示名。
    - 时间分桶用 SQL strftime 直接做，不拉全表。
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime

import mcp_server


def _parse_date(s, end=False):
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"日期格式错误（要 YYYY-MM-DD）: {s}")
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


BUCKET_FORMATS = {
    "year": "%Y",
    "month": "%Y-%m",
    "week": "%Y-W%W",
    "all": None,
}


def _build_where(start_ts, end_ts):
    clauses, params = [], []
    if start_ts is not None:
        clauses.append("create_time >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("create_time <= ?")
        params.append(end_ts)
    return clauses, params


def _query_shard(db_path, table_name, bucket_fmt, start_ts, end_ts,
                 exclude_system=True):
    """返回 [(bucket_label, sender_id, n), ...]。bucket_label 为 None 表示不分桶。

    exclude_system=True 时排除：
      - base_type 10000 (系统消息: 入群/退群/拍一拍文本/群公告等)
      - base_type 10002 (撤回提示)
      - base_type 49 中的"通知类" app subtype:
          62 = 拍一拍, 2000 = 转账, 2001 = 红包, 2003 = 红包封面
        这些 sender 经常被微信置成系统占位，rowid 不在 Name2Id；且不算真人发言。
    local_type 高 32 位是 app subtype，base_type = local_type & 0xFFFFFFFF
    （见 mcp_server._split_msg_type）。
    """
    clauses, params = _build_where(start_ts, end_ts)
    if exclude_system:
        # SQLite 的 & 是位与，对超 32 位整型也没问题
        clauses.append("(local_type & 0xFFFFFFFF) NOT IN (10000, 10002)")
        # 排除 base=49 且 app subtype 是拍一拍/红包/转账类
        clauses.append(
            "NOT ((local_type & 0xFFFFFFFF) = 49 "
            "AND (local_type >> 32) IN (62, 2000, 2001, 2003))"
        )
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    if bucket_fmt:
        select = (f"strftime('{bucket_fmt}', create_time, 'unixepoch', 'localtime') AS bucket, "
                  f"real_sender_id, COUNT(*)")
        group = "GROUP BY bucket, real_sender_id"
    else:
        select = "NULL AS bucket, real_sender_id, COUNT(*)"
        group = "GROUP BY real_sender_id"

    sql = f"SELECT {select} FROM [{table_name}] {where_sql} {group}"
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
        # 同时要这个 shard 的 Name2Id（rowid -> username）
        try:
            id2u = {rid: uname for rid, uname in conn.execute(
                "SELECT rowid, user_name FROM Name2Id").fetchall() if uname}
        except sqlite3.DatabaseError:
            id2u = {}
    return rows, id2u


def collect_group_stats(group_name, bucket="month", start_ts=None, end_ts=None,
                         exclude_system=True):
    """返回 dict:
        {
          'group': display_name, 'username': '...@chatroom',
          'bucket': 'month' | ...,
          'totals': {sender_username: count},
          'buckets': {bucket_label: {sender_username: count}},
          'names':  {sender_username: display_name},
          'unknown_sources': {placeholder_uname: [(db_path, table_name, sender_id), ...]},
        }
    """
    ctx = mcp_server._resolve_chat_context(group_name)
    if not ctx:
        sys.exit(f"找不到群: {group_name}")
    if not ctx["is_group"]:
        sys.exit(f"{ctx['display_name']} 不是群（username={ctx['username']}）。本脚本只统计群。")
    if not ctx["message_tables"]:
        sys.exit(f"群 {ctx['display_name']} 没有消息表（可能从未收发过消息）")

    bucket_fmt = BUCKET_FORMATS[bucket]
    contact_names = mcp_server.get_contact_names()
    self_username = mcp_server._get_self_username()

    # 先逐 shard 取计数 + Name2Id；解析推迟到所有 shard 扫完之后，这样可以用
    # 跨 shard 的 Name2Id 并集补救本 shard 缺失的 rowid（rowid 在不同 shard 之间
    # 不通用，但在 message DB 这一层经常会出现引用了"曾经存在"的 rowid 的情况）。
    per_shard = []  # [(db_path, table_name, rows, id2u_local)]
    for tbl in ctx["message_tables"]:
        rows, id2u_local = _query_shard(tbl["db_path"], tbl["table_name"],
                                        bucket_fmt, start_ts, end_ts,
                                        exclude_system=exclude_system)
        per_shard.append((tbl["db_path"], tbl["table_name"], rows, id2u_local))

    # 不做跨 shard 并集！rowid 在不同 shard 不通用，强行兜底会把 A shard 的映射
    # 套到 B shard 的消息，导致"群里没出现过的人"被错误归因。本 shard 查不到就
    # 保留 _unknown_id_<rowid>，由 --show-unknown 输出样本人工识别。
    totals = defaultdict(int)
    buckets = defaultdict(lambda: defaultdict(int))
    unknown_sources = defaultdict(list)

    for db_path, table_name, rows, id2u_local in per_shard:
        for bucket_label, sender_id, n in rows:
            uname = id2u_local.get(sender_id)
            if not uname:
                uname = f"_unknown_id_{sender_id}@{os.path.basename(db_path)}"
                unknown_sources[uname].append((db_path, table_name, sender_id))
            totals[uname] += n
            if bucket_label is not None:
                buckets[bucket_label][uname] += n

    names_map = {}
    for uname in totals:
        if uname == self_username:
            display = "我"
        elif uname.startswith("_unknown_id_"):
            display = uname
        else:
            display = contact_names.get(uname, uname)
        names_map[uname] = display

    return {
        "group": ctx["display_name"],
        "username": ctx["username"],
        "bucket": bucket,
        "totals": dict(totals),
        "buckets": {k: dict(v) for k, v in sorted(buckets.items())},
        "names": names_map,
        "unknown_sources": {k: list(v) for k, v in unknown_sources.items()},
    }


def _fmt_pct(n, total):
    return f"{(n / total * 100):.1f}%" if total else "0.0%"


def print_table(stats, top):
    totals = stats["totals"]
    if not totals:
        print("没有发言记录")
        return
    grand = sum(totals.values())
    print(f"\n群: {stats['group']} ({stats['username']})")
    print(f"总消息数: {grand}  | 发言人数: {len(totals)}  | 分桶: {stats['bucket']}")
    print()

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    if top > 0:
        ranked = ranked[:top]

    print(f"{'#':>4}  {'消息数':>8}  {'占比':>6}  显示名 (username)")
    print("-" * 80)
    for i, (uname, n) in enumerate(ranked, 1):
        print(f"{i:>4}  {n:>8}  {_fmt_pct(n, grand):>6}  "
              f"{stats['names'][uname]} ({uname})")

    if stats["buckets"]:
        print(f"\n按 {stats['bucket']} 分布（每桶 Top 5）:")
        for label, per in stats["buckets"].items():
            sub_total = sum(per.values())
            top5 = sorted(per.items(), key=lambda kv: kv[1], reverse=True)[:5]
            parts = [f"{stats['names'][u]} {n}({_fmt_pct(n, sub_total)})" for u, n in top5]
            print(f"  [{label}] 共 {sub_total}: " + ", ".join(parts))

    n_unknown = sum(1 for u in stats["totals"] if u.startswith("_unknown_id_"))
    if n_unknown:
        unk_total = sum(n for u, n in stats["totals"].items() if u.startswith("_unknown_id_"))
        print(f"\n⚠ 有 {n_unknown} 个未识别发送方（共 {unk_total} 条消息）。"
              f"加 --show-unknown N 看样本。")


def print_unknown_samples(stats, n_per, start_ts=None, end_ts=None):
    """对每个 _unknown_id_*，从其来源 shard 抓 n_per 条样本打印出来。
    用 mcp_server 的格式化器解析 content，群消息的 sender_from_content 字段
    经常带原始 wxid，可作为人工识别线索。
    """
    print("\n" + "=" * 70)
    print(f"未识别发送方样本（每人最多 {n_per} 条）")
    print("=" * 70)

    chat_username = stats["username"]
    chat_display = stats["group"]
    contact_names = mcp_server.get_contact_names()

    where_clauses, base_params = _build_where(start_ts, end_ts)

    for uname, sources in stats["unknown_sources"].items():
        # 同一 _unknown_id_X 可能来自多个 shard；每个 shard 取一点
        per_shard_quota = max(1, n_per // max(1, len(sources)))
        printed = 0
        print(f"\n[{uname}] 总 {stats['totals'][uname]} 条；来源 shard 数: {len(sources)}")
        for db_path, table_name, sender_id in sources:
            if printed >= n_per:
                break
            quota = min(per_shard_quota, n_per - printed)
            clauses = list(where_clauses) + ["real_sender_id = ?"]
            params = list(base_params) + [sender_id]
            sql = (f"SELECT local_id, local_type, create_time, real_sender_id, "
                   f"message_content, WCDB_CT_message_content "
                   f"FROM [{table_name}] WHERE " + " AND ".join(clauses) +
                   " ORDER BY create_time ASC LIMIT ?")
            params.append(quota)
            try:
                with closing(sqlite3.connect(db_path)) as conn:
                    rows = conn.execute(sql, params).fetchall()
            except sqlite3.DatabaseError as e:
                print(f"  (跳过 {os.path.basename(db_path)}::{table_name}: {e})")
                continue
            print(f"  -- shard: {os.path.basename(db_path)} sender_id={sender_id} --")
            for r in rows:
                local_id, local_type, ts, _sid, content, ct = r
                decoded = mcp_server._decompress_content(content, ct)
                text, _ = mcp_server._format_message_text(
                    local_id, local_type, decoded, True, chat_username, chat_display, contact_names
                )
                tstr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
                print(f"    [{tstr}] type={local_type} {text!r:.200}")
                printed += 1
                if printed >= n_per:
                    break


# --------- HTML 可视化 ---------

HTML_TEMPLATE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>群发言统计 - {group}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; margin: 24px; color: #222; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 18px; }}
  .row {{ display: flex; gap: 24px; flex-wrap: wrap; }}
  .card {{ flex: 1 1 480px; min-width: 480px; height: 520px; border: 1px solid #eee;
           border-radius: 6px; padding: 8px; }}
  table {{ border-collapse: collapse; margin-top: 18px; font-size: 13px; }}
  th, td {{ border: 1px solid #eee; padding: 4px 10px; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  thead {{ background: #fafafa; }}
</style>
</head>
<body>
<h1>群发言统计 — {group}</h1>
<div class="meta">username: {username} · 总消息: {grand} · 发言人: {n_users} · 分桶: {bucket}{time_range}</div>

<div class="row">
  <div class="card" id="pie"></div>
  <div class="card" id="stack"></div>
</div>

<h2 style="font-size:15px; margin-top:24px;">总览（前 {top} 名）</h2>
{table_html}

<script>
const data = {data_json};

// 1) 总占比饼图（前 N + Others）
const pieLabels = data.totals.map(t => t.name);
const pieValues = data.totals.map(t => t.count);
Plotly.newPlot('pie', [{{
  type: 'pie', labels: pieLabels, values: pieValues, hole: 0.35, sort: false,
  textinfo: 'label+percent', textposition: 'inside'
}}], {{ title: '总发言占比 (Top {top} + 其他)', margin: {{t: 40}}, showlegend: false }},
   {{ responsive: true }});

// 2) 按时间桶的堆叠柱状图
if (data.buckets && data.buckets.length) {{
  const xs = data.buckets.map(b => b.label);
  const traces = data.top_users.map(u => ({{
    type: 'bar', name: u.name,
    x: xs, y: data.buckets.map(b => b.counts[u.username] || 0),
  }}));
  // Others 合并
  const otherTrace = {{
    type: 'bar', name: '其他',
    x: xs, y: data.buckets.map(b => b.others_count),
    marker: {{ color: '#bbb' }},
  }};
  Plotly.newPlot('stack', [...traces, otherTrace], {{
    barmode: 'stack',
    title: `按 {bucket} 分布`, margin: {{t: 40}},
    xaxis: {{ tickangle: -30 }},
    legend: {{ orientation: 'h', y: -0.25 }},
  }}, {{ responsive: true }});
}} else {{
  document.getElementById('stack').innerHTML =
    '<div style="padding:40px;color:#888;">未启用时间分桶（--bucket all）</div>';
}}
</script>
</body>
</html>
"""


def _build_table_html(ranked, names, grand):
    rows = ["<table><thead><tr><th>#</th><th>显示名</th><th>username</th>"
            "<th>消息数</th><th>占比</th></tr></thead><tbody>"]
    for i, (u, n) in enumerate(ranked, 1):
        pct = _fmt_pct(n, grand)
        rows.append(f"<tr><td>{i}</td><td>{names[u]}</td><td>{u}</td>"
                    f"<td>{n}</td><td>{pct}</td></tr>")
    rows.append("</tbody></table>")
    return "\n".join(rows)


def render_html(stats, output_path, top, time_range_str):
    totals = stats["totals"]
    grand = sum(totals.values())
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    top_list = ranked[:top] if top > 0 else ranked
    top_unames = {u for u, _ in top_list}

    # 饼图：top + others
    others_n = sum(n for u, n in ranked if u not in top_unames)
    pie_totals = [{"name": stats["names"][u], "count": n} for u, n in top_list]
    if others_n > 0:
        pie_totals.append({"name": "其他", "count": others_n})

    # 堆叠柱：每桶里 top 用户的明细 + others 汇总
    bucket_data = []
    for label, per in stats["buckets"].items():
        others = sum(n for u, n in per.items() if u not in top_unames)
        counts = {u: per.get(u, 0) for u in top_unames}
        bucket_data.append({"label": label, "counts": counts, "others_count": others})

    payload = {
        "totals": pie_totals,
        "top_users": [{"username": u, "name": stats["names"][u]} for u, _ in top_list],
        "buckets": bucket_data,
    }

    html = HTML_TEMPLATE.format(
        group=stats["group"],
        username=stats["username"],
        grand=grand,
        n_users=len(totals),
        bucket=stats["bucket"],
        top=len(top_list),
        time_range=time_range_str,
        data_json=json.dumps(payload, ensure_ascii=False),
        table_html=_build_table_html(top_list, stats["names"], grand),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser(description="群成员发言占比统计 + 时间分桶 + HTML 可视化")
    ap.add_argument("group", help="群名（备注/昵称/wxid 都行，模糊匹配走 mcp_server.resolve_username）")
    ap.add_argument("--bucket", choices=list(BUCKET_FORMATS.keys()), default="month",
                    help="时间分桶粒度，默认 month。all = 不分桶")
    ap.add_argument("--top", type=int, default=15, help="保留前 N 名（HTML/表格），默认 15")
    ap.add_argument("--since", help="起始日期 YYYY-MM-DD（含）")
    ap.add_argument("--until", help="结束日期 YYYY-MM-DD（含）")
    ap.add_argument("--json", help="同时写出 JSON")
    ap.add_argument("--html", help="同时写出 HTML 可视化（单文件，依赖 Plotly CDN）")
    ap.add_argument("--show-unknown", type=int, default=0, metavar="N",
                    help="对未识别的 _unknown_id_*，每个打印 N 条样本消息（含解析后的内容）")
    ap.add_argument("--include-system", action="store_true",
                    help="把系统消息(10000)/撤回(10002)也算进发言。默认排除。")
    args = ap.parse_args()

    start_ts = _parse_date(args.since)
    end_ts = _parse_date(args.until, end=True)

    stats = collect_group_stats(args.group, bucket=args.bucket,
                                start_ts=start_ts, end_ts=end_ts,
                                exclude_system=not args.include_system)
    print_table(stats, args.top)

    if args.show_unknown > 0 and stats["unknown_sources"]:
        print_unknown_samples(stats, args.show_unknown,
                              start_ts=start_ts, end_ts=end_ts)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        print(f"\nJSON -> {args.json}")

    if args.html:
        time_range_str = ""
        if args.since or args.until:
            time_range_str = f" · 时间: {args.since or '-inf'} ~ {args.until or 'now'}"
        render_html(stats, args.html, args.top, time_range_str)
        print(f"HTML -> {args.html}（直接在浏览器打开）")


if __name__ == "__main__":
    main()
