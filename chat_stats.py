"""
统计每个聊天的消息条数，支持按下限/时间窗口过滤。

用法:
    python3 chat_stats.py [--min N] [--top N] [--groups | --private]
                         [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                         [--json out.json] [--csv out.csv]

示例:
    python3 chat_stats.py --min 1000
    python3 chat_stats.py --top 30 --groups
    python3 chat_stats.py --since 2025-01-01 --until 2026-01-01 --min 500

实现:
    扫描所有 message_N.db 中的 Msg_<md5> 表 + 每个 shard 的 Name2Id 并集，
    将 hash 表名反向映射回 username，再用 contact.db 还原成显示名。
    同一个 username 可能跨多个 shard，会做求和。

需先完成 WeChat DB 解密（详见 README）。
"""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
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
        # --until 含当日 23:59:59
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def _list_message_dbs():
    """收集所有 message_N.db 解密后的路径。

    mcp_server.MSG_DB_KEYS 已经把 fts/resource 过滤掉，这里只需要让 cache 解密
    并取出临时路径即可。cache.get() 返回的是 tmp 目录下 hash 化的文件名。
    """
    paths = []
    for rel_key in mcp_server.MSG_DB_KEYS:
        p = mcp_server._cache.get(rel_key)
        if p:
            paths.append(p)
    return paths


def _collect_msg_tables(db_path, self_username):
    """返回该 shard 的 (msg_table_names, hash_to_username, self_rowid)。

    self_rowid 是 Name2Id 中 user_name == self_username 的 rowid（int 或 None）。
    每个 shard 的 rowid 独立，必须 per-shard 解析。
    """
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()
        tables = [r[0] for r in rows if mcp_server._is_safe_msg_table_name(r[0])]

        hash_to_username = {}
        self_rowid = None
        try:
            for rowid, user_name in conn.execute(
                "SELECT rowid, user_name FROM Name2Id"
            ).fetchall():
                if not user_name:
                    continue
                h = hashlib.md5(user_name.encode()).hexdigest()
                hash_to_username[f"Msg_{h}"] = user_name
                if self_username and user_name == self_username:
                    self_rowid = rowid
        except sqlite3.Error:
            pass
    return tables, hash_to_username, self_rowid


def _count_for_table(conn, table_name, start_ts, end_ts, sender_clause, sender_params):
    where = list(sender_clause)
    params = list(sender_params)
    if start_ts is not None:
        where.append("create_time >= ?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("create_time <= ?")
        params.append(end_ts)
    sql = f"SELECT COUNT(*) FROM [{table_name}]"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql, params).fetchone()[0]


def _sum_chars_for_table(conn, table_name, start_ts, end_ts, is_group):
    """汇总该表中纯文字消息（local_type base = 1）的总字符数。

    content 字段是 zstd 压缩字节（ct=4）或 utf-8 文本，必须解压后用 Python 数 Unicode
    字符数；SQL LENGTH() 得到的是字节长度，对中文/压缩内容都不对。群聊 content 形如
    "<sender_wxid>:\n<text>"，先剥前缀再计数。
    """
    where = ["(local_type = 1 OR local_type % 4294967296 = 1)"]
    params = []
    if start_ts is not None:
        where.append("create_time >= ?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("create_time <= ?")
        params.append(end_ts)
    sql = (f"SELECT message_content, WCDB_CT_message_content FROM [{table_name}] "
           f"WHERE " + " AND ".join(where))
    total = 0
    for content, ct in conn.execute(sql, params):
        text = mcp_server._decompress_content(content, ct)
        if not text:
            continue
        if is_group and ":\n" in text:
            text = text.split(":\n", 1)[1]
        total += len(text)
    return total


def collect_stats(start_ts=None, end_ts=None):
    """返回 [{username, display, is_group, count_total, count_me, count_other,
    chars_text}, ...]，按 count_total 降序。

    chars_text 为该聊天纯文字消息（local_type base=1）解压后的 Unicode 字符总数；群聊
    会先剥掉 "<sender>:\\n" 前缀。me/other 通过每个 shard 自己的 Name2Id 把
    self_username 解析为 rowid。
    """
    db_paths = _list_message_dbs()
    if not db_paths:
        sys.exit("找不到任何 message_*.db，先运行解密")

    self_username = mcp_server._get_self_username()

    # username -> {'total': n, 'me': n, 'chars': n}
    counts = {}
    hash_to_username = {}
    # (db_path, table_name, total, me, chars) — username 还没解出来时缓存
    unresolved = []

    bad_dbs = []      # [(db_path, error_msg)]
    bad_tables = []   # [(db_path, table_name, error_msg)]

    for db_path in db_paths:
        try:
            tables, h2u, self_rowid = _collect_msg_tables(db_path, self_username)
        except sqlite3.DatabaseError as e:
            bad_dbs.append((db_path, str(e)))
            continue
        hash_to_username.update(h2u)

        with closing(sqlite3.connect(db_path)) as conn:
            for table_name in tables:
                # is_group 影响群聊前缀剥离，必须在扫描前确定
                username = h2u.get(table_name) or hash_to_username.get(table_name)
                is_group = bool(username and "@chatroom" in username)
                try:
                    total = _count_for_table(conn, table_name, start_ts, end_ts, [], [])
                    if total == 0:
                        continue
                    if self_rowid is not None:
                        me = _count_for_table(conn, table_name, start_ts, end_ts,
                                              ["real_sender_id = ?"], [self_rowid])
                    else:
                        me = 0
                    chars = _sum_chars_for_table(conn, table_name, start_ts, end_ts,
                                                 is_group)
                except sqlite3.DatabaseError as e:
                    bad_tables.append((db_path, table_name, str(e)))
                    continue

                if username:
                    bucket = counts.setdefault(username,
                                               {"total": 0, "me": 0, "chars": 0})
                    bucket["total"] += total
                    bucket["me"] += me
                    bucket["chars"] += chars
                else:
                    unresolved.append((table_name, total, me, chars))

    if bad_dbs or bad_tables:
        print("⚠ 部分 DB / 表读取失败，已跳过：", file=sys.stderr)
        for p, msg in bad_dbs:
            print(f"  [DB ] {p}: {msg}", file=sys.stderr)
        for p, t, msg in bad_tables:
            print(f"  [TBL] {p}::{t}: {msg}", file=sys.stderr)
        print("  → 这通常说明缓存解密文件损坏，可清空 %TEMP%\\wechat_mcp_cache 后重跑。",
              file=sys.stderr)

    for table_name, total, me, chars in unresolved:
        username = hash_to_username.get(table_name, table_name)
        bucket = counts.setdefault(username, {"total": 0, "me": 0, "chars": 0})
        bucket["total"] += total
        bucket["me"] += me
        bucket["chars"] += chars

    names = mcp_server.get_contact_names()
    results = []
    for username, c in counts.items():
        is_group = "@chatroom" in username
        display = names.get(username, username)
        results.append({
            "username": username,
            "display": display,
            "is_group": is_group,
            "count_total": c["total"],
            "count_me": c["me"],
            "count_other": c["total"] - c["me"],
            "chars_text": c["chars"],
        })
    results.sort(key=lambda x: x["count_total"], reverse=True)
    return results


def main():
    ap = argparse.ArgumentParser(description="按消息数统计/过滤聊天")
    ap.add_argument("--min", type=int, default=0,
                    help="按双方消息总数过滤（≥ N）；与 --min-me / --min-other 可叠加")
    ap.add_argument("--top", type=int, default=0, help="只显示前 N 个（0 = 不限）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--groups", action="store_true", help="只统计群聊")
    g.add_argument("--private", action="store_true", help="只统计 1-on-1")
    ap.add_argument("--min-me", type=int, default=0, help="只显示我发送 ≥ N 的聊天")
    ap.add_argument("--min-other", type=int, default=0, help="只显示对方发送 ≥ N 的聊天")
    ap.add_argument("--min-chars", type=int, default=0,
                    help="只显示纯文字消息总字数 ≥ N 的聊天")
    ap.add_argument("--sort", choices=["total", "me", "other", "chars"], default="total",
                    help="排序口径，默认 total（双方总数）")
    ap.add_argument("--since", help="起始日期 YYYY-MM-DD（含）")
    ap.add_argument("--until", help="结束日期 YYYY-MM-DD（含）")
    ap.add_argument("--json", help="同时写出 JSON")
    ap.add_argument("--csv", help="同时写出 CSV")
    args = ap.parse_args()

    start_ts = _parse_date(args.since)
    end_ts = _parse_date(args.until, end=True)

    results = collect_stats(start_ts, end_ts)

    if args.groups:
        results = [r for r in results if r["is_group"]]
    if args.private:
        results = [r for r in results if not r["is_group"]]
    if args.min > 0:
        results = [r for r in results if r["count_total"] >= args.min]
    if args.min_me > 0:
        results = [r for r in results if r["count_me"] >= args.min_me]
    if args.min_other > 0:
        results = [r for r in results if r["count_other"] >= args.min_other]
    if args.min_chars > 0:
        results = [r for r in results if r["chars_text"] >= args.min_chars]

    sort_key = {"total": "count_total", "me": "count_me",
                "other": "count_other", "chars": "chars_text"}[args.sort]
    results.sort(key=lambda x: x[sort_key], reverse=True)

    if args.top > 0:
        results = results[: args.top]

    total_total = sum(r["count_total"] for r in results)
    total_me = sum(r["count_me"] for r in results)
    total_chars = sum(r["chars_text"] for r in results)
    print(f"匹配 {len(results)} 个聊天 | 总消息 {total_total} | 我发送 {total_me} "
          f"| 文字字数 {total_chars} | 排序: {args.sort}")
    if start_ts or end_ts:
        s = args.since or "-inf"
        e = args.until or "now"
        print(f"时间窗口: {s} ~ {e}")
    print()
    print(f"{'#':>4}  {'类型':<4}  {'总数':>8}  {'我发':>8}  {'对方':>8}  "
          f"{'字数':>10}  显示名 (username)")
    print("-" * 100)
    for i, r in enumerate(results, 1):
        kind = "群" if r["is_group"] else "私"
        line = (f"{i:>4}  {kind:<4}  {r['count_total']:>8}  "
                f"{r['count_me']:>8}  {r['count_other']:>8}  "
                f"{r['chars_text']:>10}  "
                f"{r['display']} ({r['username']})")
        print(line)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\nJSON -> {args.json}")
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["rank", "kind", "count_total", "count_me", "count_other",
                       "chars_text", "display", "username"])
            for i, r in enumerate(results, 1):
                w.writerow([i, "group" if r["is_group"] else "private",
                           r["count_total"], r["count_me"], r["count_other"],
                           r["chars_text"], r["display"], r["username"]])
        print(f"CSV -> {args.csv}")


if __name__ == "__main__":
    main()
