"""消息类型、时区等常量定义"""
from datetime import timezone, timedelta

TZ = timezone(timedelta(hours=8))  # CST
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16

# 消息类型 → 中文名称
MSG_TYPES_CN = {
    1: "文本", 3: "图片", 6: "文件", 34: "语音", 42: "名片",
    43: "视频", 47: "表情", 48: "位置", 49: "链接/应用",
    50: "网络电话", 10000: "系统消息", 10002: "系统消息",
}

# 消息类型 → 排序权重（用于图表颜色一致性）
MSG_TYPE_ORDER = ["文本", "图片", "文件", "语音", "视频", "链接/应用", "表情", "网络电话", "名片", "位置", "系统消息"]

# 消息类型 → 图表颜色
MSG_TYPE_COLORS = {
    "文本": "#4fc3f7", "图片": "#ff8a65", "文件": "#e6a23c", "语音": "#66bb6a",
    "视频": "#f06292", "链接/应用": "#ffd54f", "表情": "#ba68c8",
    "网络电话": "#90a4ae", "名片": "#a1887f", "位置": "#4db6ac",
    "系统消息": "#78909c",
}

# 状态码
STATUS_NAMES = {1: "", 2: "已发送", 3: "已接收", 4: "系统"}

# 星期名称
DOW_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def msg_type_cn(local_type):
    """将 local_type 转为中文名称。高位标志位先 & 0xFFFFFFFF。"""
    base = local_type & 0xFFFFFFFF
    return MSG_TYPES_CN.get(base, f"未知类型({base})")
