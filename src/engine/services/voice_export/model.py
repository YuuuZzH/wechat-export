# -*- coding: utf-8 -*-
"""语音导出的数据模型、命名规则与合并计划（纯数据逻辑：无 IO、无子进程）。"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from engine.constants import TZ

#: 微信语音解码出的采样率。本机实测 8/8 条为 24 kHz（见设计文档 §2）。
DEFAULT_SAMPLE_RATE = 24000
SAMPLE_RATES = (16000, 24000)

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str, max_len: int = 80) -> str:
    """清洗成可用作文件名/目录名的字符串（保留中文与 emoji）。"""
    s = _ILLEGAL.sub('_', str(name or ''))
    s = re.sub(r'\s+', ' ', s).strip()
    s = s.strip('. ')
    if not s:
        return 'unknown'
    return s[:max_len].strip('. ') or 'unknown'


def voice_basename(sender_name: str, create_time: int) -> str:
    """形态③的文件名：``<姓名>_YYYYMMDD-HHMMSS``（本地时区，不含扩展名）。"""
    dt = datetime.fromtimestamp(int(create_time), tz=TZ)
    return '%s_%s' % (safe_filename(sender_name or 'unknown'), dt.strftime('%Y%m%d-%H%M%S'))


def pick_sample_rate(pcm_len: int, xml_ms: int) -> int:
    """按 PCM 长度与 XML 时长（毫秒）挑最可能的采样率。

    XML 时长比 PCM 实际长度长约 2%（解码会裁首尾），因此只当"哪个采样率更接近"的裁判；
    拿不到 XML 时长时用默认值 24000。
    """
    if not xml_ms or pcm_len <= 0:
        return DEFAULT_SAMPLE_RATE
    want_s = xml_ms / 1000.0
    best, best_err = DEFAULT_SAMPLE_RATE, None
    for sr in SAMPLE_RATES:
        err = abs(pcm_len / 2.0 / sr - want_s)
        if best_err is None or err < best_err:
            best, best_err = sr, err
    return best


@dataclass
class VoiceItem:
    """一条语音消息（以及它在导出过程中的派生信息）。"""

    chat_id: str
    chat_name: str
    local_id: int
    create_time: int
    duration_ms: int = 0            # XML voicelength（展示兜底 + 采样率判定）
    sender_id: str = ''             # wxid（可能为空）
    sender_name: str = ''           # 备注名 / 昵称 / '我'
    silk_path: str = ''             # 抽取到的 .silk；空 = 缺失
    pcm_path: str = ''              # 解码后的 PCM 缓存路径（见 pcm_cache，避免重复解码）
    sample_rate: int = DEFAULT_SAMPLE_RATE
    duration_s: float = 0.0         # **以 PCM 长度为准**
    out_name: str = ''              # 写出后的文件名（不含扩展名）
    offset_start: float = 0.0       # 在合并文件中的起止秒数（含段间静音）
    offset_end: float = 0.0
    transcript: str = ''
    error: str = ''

    @property
    def datetime_text(self) -> str:
        return datetime.fromtimestamp(int(self.create_time), tz=TZ).strftime('%Y-%m-%d %H:%M:%S')


@dataclass
class MergeGroup:
    """一个合并输出单元（一个人 / 一个会话 / 一天的语音）。"""

    key: str
    label: str
    items: list = field(default_factory=list)
    sample_rate: int = DEFAULT_SAMPLE_RATE
    file_stem: str = ''
    bucket: str = ''
    chunk_idx: int = 1

    @property
    def duration_s(self) -> float:
        return sum(i.duration_s for i in self.items)


def _major_rate(items) -> int:
    counts = {}
    for it in items:
        counts[it.sample_rate] = counts.get(it.sample_rate, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _bucket_of(create_time: int, split: str) -> str:
    dt = datetime.fromtimestamp(int(create_time), tz=TZ)
    return dt.strftime('%Y%m%d') if split == 'per-day' else dt.strftime('%Y%m')


def _merge_stem(group: MergeGroup, split: str) -> str:
    name = safe_filename(group.label or 'unknown')
    if split in ('per-day', 'per-month'):
        return safe_filename('%s_%s' % (name, group.bucket))
    if split == 'per-n':
        return safe_filename('%s_第%d批' % (name, group.chunk_idx))
    first = min(i.create_time for i in group.items)
    last = max(i.create_time for i in group.items)
    d1 = datetime.fromtimestamp(first, tz=TZ).strftime('%Y%m%d')
    d2 = datetime.fromtimestamp(last, tz=TZ).strftime('%Y%m%d')
    span = d1 if d1 == d2 else '%s-%s' % (d1, d2)
    return safe_filename('%s_全部语音_%s' % (name, span))


def plan_merge(items, merge_by: str = 'person', split: str = 'single',
               per_n: int = 200, gap_s: float = 1.0):
    """生成合并计划，并**就地**写入每条语音在合并文件中的起止偏移（秒）。

    * ``merge_by``：``person`` / ``chat`` / ``none``（每条一个组）
    * ``split``：``single`` / ``per-day`` / ``per-month`` / ``per-n``
    * ``gap_s``：段间静音秒数（必须与写盘时使用的值一致）
    * ``offset_end`` 不含其后的静音：``offset_end = offset_start + duration_s``
    """
    usable = [i for i in items if i.duration_s > 0 and not i.error]
    if not usable:
        return []
    usable.sort(key=lambda i: (i.create_time, i.local_id))

    groups = []
    if merge_by == 'none':
        for it in usable:
            groups.append(MergeGroup(key=it.sender_name or 'unknown',
                                     label=it.sender_name or 'unknown', items=[it]))
    else:
        key_of = (lambda i: i.sender_name or i.sender_id or 'unknown') if merge_by == 'person' \
            else (lambda i: i.chat_name or i.chat_id or 'unknown')
        buckets = {}
        for it in usable:
            bucket = _bucket_of(it.create_time, split) if split in ('per-day', 'per-month') else ''
            buckets.setdefault((key_of(it), bucket), []).append(it)
        for (key, bucket) in sorted(buckets, key=lambda k: str(k)):
            chunk_list = buckets[(key, bucket)]
            if split == 'per-n':
                chunks = [chunk_list[i:i + per_n] for i in range(0, len(chunk_list), per_n)]
            else:
                chunks = [chunk_list]
            for idx, chunk in enumerate(chunks):
                groups.append(MergeGroup(key=key, label=key, items=chunk,
                                         sample_rate=_major_rate(chunk),
                                         bucket=bucket, chunk_idx=idx + 1))

    for group in groups:
        position = 0.0
        for idx, it in enumerate(group.items):
            if idx:
                position += gap_s
            it.offset_start = position
            it.offset_end = position + it.duration_s
            position = it.offset_end
            it.out_name = voice_basename(it.sender_name, it.create_time)
        group.file_stem = _merge_stem(group, split)
    return groups
