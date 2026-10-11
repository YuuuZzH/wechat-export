"""按人批量导出语音留言。

核心层是**纯 Python**（不依赖 Flask / CLI）：Web 与命令行只是薄壳，便于独立测试。

分层：

* :mod:`model`   —— 数据模型、命名规则、合并计划
* :mod:`audio`   —— SILK→PCM→WAV/MP3/M4A、重采样、拼接、能力探测
* :mod:`collect` —— 枚举语音消息并按人归属（分片 ``Name2Id`` 权威）
* :mod:`layout`  —— 逐条/合并写盘、清单、缺失清单、zip
* :mod:`html`    —— 可脱离本程序单独打开的 HTML（零外部依赖）
* :mod:`pipeline`—— 编排、进度、取消、报告
"""

from .audio import capabilities  # noqa: F401
from .collect import collect_voice_items  # noqa: F401
from .model import (VoiceItem, MergeGroup, safe_filename, voice_basename,  # noqa: F401
                    pick_sample_rate, plan_merge, DEFAULT_SAMPLE_RATE)
from .pipeline import export_voices  # noqa: F401

__all__ = ['capabilities', 'collect_voice_items', 'export_voices', 'VoiceItem', 'MergeGroup',
           'safe_filename', 'voice_basename', 'pick_sample_rate', 'plan_merge',
           'DEFAULT_SAMPLE_RATE']
