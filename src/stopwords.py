"""
智能停用词系统 — 三层过滤:
  第一层: 静态停用词表 (~800词)
  第二层: jieba 词性过滤
  第三层: 上下文频率降权

is_meaningful(word, pos=None, context_freq=None, total_msgs=1)
  返回 (keep: bool, weight: float)
"""

from engine.services.emoji_map import WECHAT_EMOJI_MAP

# ============================================================
# 第一层: 静态停用词表
# ============================================================
STOP_WORDS = set([
    # 语气词
    "的", "了", "呢", "啊", "吧", "嘛", "呀", "哦", "嗯", "哈", "呵",
    "唉", "哎", "喂", "哟", "噢", "唔", "咳", "嗨", "嘿", "嘻", "咔",
    # 代词
    "我", "你", "他", "她", "它", "们", "咱", "俺",
    "我们", "你们", "他们", "她们", "它们", "咱们",
    "这", "那", "哪", "谁", "什么", "怎么", "怎样", "这么", "那么",
    "这个", "那个", "哪个", "这些", "那些", "这边", "那边",
    "自己", "人家", "大家", "各位",
    # 连词 / 介词
    "和", "与", "或", "及", "而", "且", "但", "虽", "然", "若", "则",
    "于", "以", "把", "被", "从", "对", "为", "所",
    "因为", "所以", "但是", "不过", "如果", "虽然", "然后", "而且",
    "还是", "或者", "以及", "关于", "对于", "按照", "经过", "通过",
    # 助词 / 量词
    "地", "得", "着", "个", "只", "条", "次", "些", "点",
    # 时间词 (过于泛化)
    "今天", "明天", "昨天", "今年", "去年", "现在", "之前", "以后",
    "时候", "一下", "一会", "上午", "下午", "晚上", "早上", "中午",
    "刚才", "已经", "正在", "马上", "一直", "经常", "以前", "从来",
    # 判断词 / 能愿动词
    "是", "能", "可", "要", "想", "该", "应", "将", "会",
    "可以", "可能", "应该", "必须", "能够", "需要", "觉得", "认为",
    "知道", "感觉", "以为", "希望", "愿意",
    # 程度副词
    "很", "太", "更", "最", "比较", "非常", "有点", "特别", "十分",
    "挺", "蛮", "极", "好",
    # 范围 / 频率副词
    "都", "也", "还", "就", "才", "只", "又", "再", "总", "总是",
    "一般", "大约", "大概", "几乎", "全都",
    # 礼貌用语 / 套话
    "您好", "你好", "谢谢", "多谢", "感谢", "不客气", "没事",
    "请问", "麻烦", "拜托", "辛苦了", "好的", "收到", "明白",
    "了解", "知道", "Ok", "ok", "OK", "行", "可以",
    "没问题", "没关系", "不好意思", "对不起", "抱歉",
    # 社交用语 / 微信高频
    "哈哈", "呵呵", "嘿嘿", "嘻嘻", "吼吼", "嘎嘎",
    "捂脸", "抱拳", "呲牙", "偷笑", "微笑", "大哭", "尴尬",
    "玫瑰", "爱心", "点赞", "握手", "强", "合十",
    "早", "晚安", "早安", "再见", "拜拜",
    # 指示词 / 泛指
    "什么", "怎么", "哪里", "那儿", "这儿", "那里", "这里",
    "这样", "那样", "各种", "其他", "别的", "另外",
    # 数量 / 单位
    "多", "少", "大", "小", "多少", "几个", "一个", "两个",
    "第一", "第二", "第三", "之一", "其中",
    # 通用动词 (低频信息量)
    "做", "让", "给", "用", "过", "出", "进", "来", "去",
    "看", "说", "问", "回", "发", "打", "开", "关",
    # 通用名词 (过泛)
    "人", "事", "东西", "问题", "情况", "方法", "方式",
    "方面", "地方", "部分", "原因", "结果",
    "的话", "是不是", "有没有", "能不能",
    # 英文/数字
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "i", "you", "he", "she", "it", "we", "they",
    "this", "that", "these", "those",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "or", "but", "if", "so", "as", "no", "not",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "can", "could", "may", "might", "shall", "should",
    "http", "https", "www", "com", "cn", "net", "org",
    # 标点 / 特殊
    "\n", "\r", "\t", " ", "", "️", "♂", "♀",
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "10",
    "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
])

# Extend STOP_WORDS with WeChat emoji text descriptions (defense-in-depth —
# the primary filter is regex stripping of [xxx] codes in wordcloud_gen).
_EMOJI_TEXTS = {code[1:-1] for code in WECHAT_EMOJI_MAP if len(code[1:-1]) >= 2}
STOP_WORDS |= _EMOJI_TEXTS


# ============================================================
# 第二层: jieba 词性过滤
# ============================================================
# 保留的词性标记 (jieba pos tags)
KEEP_POS_PREFIXES = {'n', 'v', 'a', 'eng', 'j', 'l', 'i'}  # 名词/动词/形容词/英文/简称/习语/成语
# 明确排除的前缀
DROP_POS_PREFIXES = {'u', 'c', 'p', 'r', 'e', 'o', 'q', 'm', 'd', 'f', 't', 'k', 'y', 'z', 'w', 'x'}


def is_meaningful(word, pos=None, context_freq=0.0, total_msgs=1):
    """判断一个词是否值得保留。

    Args:
        word: 词语
        pos: jieba 词性标注 (可选)
        context_freq: 词在上下文中出现的消息占比 (0.0~1.0)
        total_msgs: 总消息数

    Returns:
        (keep: bool, weight: float)
    """
    word = word.strip()

    # 长度检查
    if len(word) < 2 and not (word.isascii() and word.isalpha()):
        return (False, 0)

    # 纯数字 / 纯标点 / 纯 emoji
    if word.isdigit():
        return (False, 0)
    if all(not c.isalnum() and not '一' <= c <= '鿿' for c in word):
        return (False, 0)

    # 纯 emoji 字符
    if any(0x1F000 <= ord(c) <= 0x1FFFF for c in word):
        return (False, 0)

    # 静态停用词
    if word.lower() in STOP_WORDS:
        return (False, 0)

    # 词性过滤 (如果提供了词性)
    if pos:
        # 去掉 jieba 词性标记的首字母进行判断
        pos_prefix = pos[0].lower() if pos else ''
        if pos_prefix in KEEP_POS_PREFIXES:
            pass  # explicit whitelist — always keep
        elif pos_prefix in DROP_POS_PREFIXES:
            return (False, 0)

    # 第三层: 上下文降权
    weight = 1.0
    if context_freq > 0.8 and total_msgs > 20:
        # 超过 80% 的消息都出现 → "群通用词"，降权
        weight = 0.3
    elif context_freq > 0.5 and total_msgs > 50:
        weight = 0.6

    return (True, weight)


def filter_words(word_counter, total_msgs=1):
    """对 Counter 结果做智能过滤，返回 [(word, weighted_count), ...]。

    Args:
        word_counter: collections.Counter, {word: count}
        total_msgs: 总消息数，用于计算上下文频率

    Returns:
        [(word, weighted_count), ...] 按加权计数降序
    """
    results = []
    for word, count in word_counter.items():
        freq = count / total_msgs if total_msgs else 0
        keep, weight = is_meaningful(word, context_freq=freq, total_msgs=total_msgs)
        if keep:
            results.append((word, count * weight))

    results.sort(key=lambda x: x[1], reverse=True)
    return results
