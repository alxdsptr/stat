"""
基于 export_chat.py 产出的 JSON 生成聊天词云。

用法:
    python3 wordcloud_chat.py <export.json> [-o out.png] [--sender me|other|all]
                              [--min-len 2] [--top 30] [--font <font_path>]
                              [--stopwords <file>]

示例:
    python3 wordcloud_chat.py cjy_export.json
    python3 wordcloud_chat.py cjy_export.json --sender me -o cjy_me.png
    python3 wordcloud_chat.py 群名_export.json --sender other --top 50

依赖:
    pip install jieba wordcloud matplotlib

中文字体: Windows 默认尝试 C:\\Windows\\Fonts\\msyh.ttc；macOS 试 PingFang；
Linux 试 Noto Sans CJK。找不到时通过 --font 显式指定 .ttf/.ttc 路径。
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

try:
    import jieba
except ImportError:
    sys.exit("缺少依赖: pip install jieba wordcloud matplotlib")


# 表情占位符 [旺柴] [抱拳] 等，分词前剥掉
EMOJI_TAG_RE = re.compile(r"\[[^\[\]]{1,8}\]")
# 只保留中文 / 英文 / 数字 token
TOKEN_RE = re.compile(r"^[一-鿿_a-zA-Z0-9]+$")

DEFAULT_STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "她", "它", "在", "也", "和", "就", "都",
    "不", "没", "有", "啊", "吧", "吗", "呢", "嗯", "哦", "嘿", "哈", "哈哈",
    "哈哈哈", "哈哈哈哈", "这", "那", "这个", "那个", "什么", "怎么", "可以",
    "一个", "一下", "已经", "现在", "还是", "但是", "可是", "因为", "所以",
    "如果", "应该", "觉得", "知道", "感觉", "好的", "嗯嗯", "好", "对", "我们",
    "你们", "他们", "自己", "时候", "今天", "明天", "昨天", "一", "二", "三",
    "the", "a", "an", "is", "are", "to", "of", "and", "or", "in", "on", "for",
    "it", "this", "that", "be", "i", "you", "we", "they",
}

DEFAULT_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]


def find_font():
    for p in DEFAULT_FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def load_messages(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("messages", []), data.get("chat", os.path.basename(path))


def collect_text(messages, sender_filter):
    """sender_filter: 'me' | 'other' | 'all'"""
    texts = []
    skipped_types = {"voice", "image", "video", "sticker", "system", "recall",
                     "location", "call", "contact_card", "link_or_file"}
    for m in messages:
        msg_type = m.get("type", "text")
        if msg_type in skipped_types:
            # 语音可能有 transcription，捞一下
            tr = m.get("transcription")
            if tr:
                texts.append((m.get("sender", ""), tr))
            continue
        content = m.get("content")
        if not content:
            continue
        sender = m.get("sender", "")
        if sender_filter == "me" and sender != "me":
            continue
        if sender_filter == "other" and sender == "me":
            continue
        texts.append((sender, content))
    return texts


def tokenize(texts, stopwords, min_len):
    counter = Counter()
    for _, text in texts:
        text = EMOJI_TAG_RE.sub(" ", text)
        for tok in jieba.cut(text):
            tok = tok.strip()
            if len(tok) < min_len:
                continue
            if tok in stopwords:
                continue
            if not TOKEN_RE.match(tok):
                continue
            counter[tok] += 1
    return counter


def render_wordcloud(counter, output_path, font_path, title):
    if not counter:
        sys.exit("没有可用词汇，检查 --sender / --min-len / 输入文件")
    try:
        from wordcloud import WordCloud
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("缺少依赖: pip install wordcloud matplotlib")

    wc = WordCloud(
        font_path=font_path,
        width=1600,
        height=1000,
        background_color="white",
        max_words=400,
        collocations=False,
    ).generate_from_frequencies(counter)

    fig, ax = plt.subplots(figsize=(16, 10))
    ax.imshow(wc, interpolation="bilinear")
    ax.set_axis_off()
    ax.set_title(title, fontsize=14)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description="生成微信聊天词云")
    ap.add_argument("input", help="export_chat.py 产出的 JSON 文件")
    ap.add_argument("-o", "--output", help="输出 PNG 路径，默认 <input>_wordcloud.png")
    ap.add_argument("--sender", choices=["me", "other", "all"], default="all",
                    help="只统计 me / 对方(other) / 全部 (默认 all)")
    ap.add_argument("--min-len", type=int, default=2, help="最短词长度，默认 2")
    ap.add_argument("--top", type=int, default=30, help="终端打印前 N 词，默认 30")
    ap.add_argument("--font", help="中文字体路径（.ttf/.ttc）")
    ap.add_argument("--stopwords", help="额外停用词文件，每行一个词")
    args = ap.parse_args()

    stopwords = set(DEFAULT_STOPWORDS)
    if args.stopwords:
        with open(args.stopwords, "r", encoding="utf-8") as f:
            stopwords.update(line.strip() for line in f if line.strip())

    font_path = args.font or find_font()
    if not font_path:
        sys.exit("找不到中文字体，请用 --font 指定 .ttf/.ttc 路径")

    messages, chat = load_messages(args.input)
    texts = collect_text(messages, args.sender)
    print(f"聊天: {chat}  | 消息总数: {len(messages)}  | 参与统计: {len(texts)} 条 (sender={args.sender})")

    counter = tokenize(texts, stopwords, args.min_len)
    print(f"去停用词后词数: {sum(counter.values())}  | 词表大小: {len(counter)}")

    print(f"\nTop {args.top} 高频词:")
    for word, n in counter.most_common(args.top):
        print(f"  {word:<10} {n}")

    output_path = args.output or os.path.splitext(args.input)[0] + "_wordcloud.png"
    render_wordcloud(counter, output_path, font_path, f"{chat} 词云 ({args.sender})")
    print(f"\n词云已保存到: {output_path}")


if __name__ == "__main__":
    main()
