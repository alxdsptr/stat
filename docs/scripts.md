# 脚本说明


## chat_stats.py

按聊天维度汇总。每个聊天给出：消息总数、我发送数、对方发送数、纯文字消息总字数（解压后的 Unicode 字符数）。

### 用法

```
python3 chat_stats.py [--min N] [--top N] [--groups | --private]
                      [--min-me N] [--min-other N] [--min-chars N]
                      [--sort total|me|other|chars]
                      [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                      [--json out.json] [--csv out.csv]
```

### 选项

| 参数 | 说明 |
| --- | --- |
| `--min N` | 双方消息总数 ≥ N 才显示 |
| `--min-me N` | 我发送的消息数 ≥ N 才显示 |
| `--min-other N` | 对方发送的消息数 ≥ N 才显示 |
| `--min-chars N` | 纯文字消息总字数 ≥ N 才显示 |
| `--top N` | 只取前 N 行（0 = 不限） |
| `--groups` / `--private` | 只看群聊 / 只看 1-on-1（互斥） |
| `--sort` | 排序口径：`total`（默认）/ `me` / `other` / `chars` |
| `--since` / `--until` | 时间窗口（含端点；until 含当天 23:59:59） |
| `--json` / `--csv` | 同时落盘 |

### 例子

```bash
# 字数最多的前 20 个聊天
python3 chat_stats.py --top 20 --sort chars

# 2025 年内字数 ≥ 10000 的群
python3 chat_stats.py --groups --since 2025-01-01 --until 2025-12-31 --min-chars 10000

# 导出全部到 CSV
python3 chat_stats.py --csv stats.csv
```

### 字段

终端输出列：`# 类型 总数 我发 对方 字数 显示名 (username)`。

JSON / CSV 字段：

- `count_total`：双方消息总数
- `count_me`：我发送的消息数
- `count_other`：对方发送的消息数
- `chars_text`：纯文字消息（`local_type` base = 1）解压后剥掉群聊 `<sender>:\n` 前缀的 Unicode 字符总数
- `is_group` / `display` / `username`

### 注意

- 字数统计是 Python 端解压全表后用 `len(str)` 累加，比 COUNT(\*) 慢。聊天非常多时会有等待。
- 同一个 username 跨多个 `message_N.db` shard 会自动求和。
- 解密缓存有损坏的表会被跳过并打印到 stderr，提示清空 `%TEMP%\wechat_mcp_cache` 重跑。

---

## group_stats.py

只针对**一个群**：成员发言占比 + 按月/周/年分桶 + 单文件 HTML 可视化（饼图 + 堆叠柱）。

### 用法

```
python3 group_stats.py <群名> [--bucket month|week|year|all]
                      [--top N] [--since YYYY-MM-DD] [--until ...]
                      [--show-unknown N] [--include-system]
                      [--json out.json] [--html out.html]
```

`<群名>` 可以是群名、备注、wxid（`*@chatroom`），通过 `mcp_server.resolve_username` 模糊匹配。

### 选项

| 参数 | 说明 |
| --- | --- |
| `--bucket` | 时间分桶粒度：`month`（默认）/ `week` / `year` / `all`（不分桶，HTML 只渲染饼图） |
| `--top N` | 表格 / 图里保留前 N 名，其余合并为"其他"（默认 15） |
| `--since` / `--until` | 时间窗口 |
| `--show-unknown N` | 对未解析出 username 的发送方（占位 `_unknown_id_*`），每人打印 N 条样本消息辅助识别 |
| `--include-system` | 把系统消息（10000 入退群/拍一拍文本）、撤回（10002）、base=49 的拍一拍/红包/转账子类一并算进发言。默认排除 |
| `--json` / `--html` | 同时落盘；HTML 是单文件，依赖 Plotly CDN |

### 例子

```bash
python3 group_stats.py "我们的群"
python3 group_stats.py "我们的群" --bucket month --html group.html
python3 group_stats.py "我们的群" --bucket week --top 10 --since 2025-01-01
python3 group_stats.py "我们的群" --show-unknown 5     # 看看未识别的人都发了什么
```

### 输出

终端表格：`# 消息数 占比 显示名 (username)`，再按桶列出每桶的 Top 5。

`--html` 输出包含两个图：

- 总发言占比饼图（Top N + 其他）
- 按时间桶的堆叠柱状图（每个 Top 用户一条 trace + 其他合并）

### 关于 `_unknown_id_*`

群消息的 `real_sender_id` 是该 shard 的 `Name2Id` rowid。**rowid 在不同 shard 之间不通用**，所以脚本不会跨 shard 兜底（强行兜底会把 A shard 的人套到 B shard 的消息上）。本 shard 解析不出来就保留 `_unknown_id_<rowid>@<db_file>` 占位，加 `--show-unknown N` 抽样看消息内容就能人工识别（解析出的 sender 字段经常带原始 wxid）。

---

## wordcloud_chat.py

读 [export_chat.py](../export_chat.py) 产出的 JSON，分词后生成词云 PNG。

### 前置

```bash
pip install jieba wordcloud matplotlib

# 先导出某个聊天的 JSON
python3 export_chat.py <chat_name> chat.json
```

### 用法

```
python3 wordcloud_chat.py <export.json> [-o out.png]
                          [--sender me|other|all]
                          [--min-len 2] [--top 30]
                          [--font <font_path>] [--stopwords <file>]
```

### 选项

| 参数 | 说明 |
| --- | --- |
| `--sender` | `me`（只算我发的）/ `other`（只算对方/群成员）/ `all`（默认） |
| `--min-len N` | 最短词长度，默认 2（过滤"我"、"是"等单字噪声） |
| `--top N` | 终端打印前 N 高频词，默认 30 |
| `--font` | 中文字体 .ttf/.ttc 路径。不指定时按平台找：Windows 微软雅黑 / macOS PingFang / Linux Noto CJK |
| `--stopwords` | 额外停用词文件（每行一词），与内置中英文停用词合并 |
| `-o` | 输出 PNG，默认 `<input>_wordcloud.png` |

### 例子

```bash
python3 wordcloud_chat.py 朋友_export.json
python3 wordcloud_chat.py 朋友_export.json --sender me -o me.png
python3 wordcloud_chat.py 群_export.json --sender other --top 50
python3 wordcloud_chat.py chat.json --stopwords my_stop.txt --min-len 3
```

### 处理细节

- 跳过 voice / image / video / sticker / system / recall / location / call / contact_card / link_or_file 这些非文字消息，但**会**捞 voice 消息里 `transcription` 字段（`transcribe_chat.py` 写入的 Whisper 转录文本）。
- 表情占位 `[旺柴]` `[抱拳]` 之类在分词前剥掉。
- token 只保留中文/英文/数字（`re.match(r"^[一-鿿_a-zA-Z0-9]+$")`）。
- 默认停用词包含中英常见虚词 + "哈哈/嗯嗯/好的"等，可用 `--stopwords` 追加。