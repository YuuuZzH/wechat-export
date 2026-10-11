"""Message decoding utilities — zstd decompression and sender extraction.

This module provides the first two steps of the message pipeline:
1. decompress_content: decompress WeChat 4.x zstd-compressed message content
2. split_sender_prefix: extract sender wxid from group message prefix
"""

import threading

# WeChat 4.x zstd compression magic — zstd frame header
_ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'

# 每个线程各持一个解压上下文（懒创建），见 get_zstd_decompressor 的说明。
_ZSTD_LOCAL = threading.local()


def get_zstd_decompressor():
    """取**当前线程**的 zstd 解压上下文（懒创建）。

    ⚠️ `zstandard.ZstdDecompressor` **不是线程安全的**：多个线程共用同一个上下文时，
    解压会返回空结果，甚至直接触发访问违规把整个进程打崩（0xC0000005）。
    真机复现路径：在 Web 里连续点两次「语音导出」⇒ 两个导出线程同时解压 ⇒ 进程退出。

    这里给每个线程一个独立上下文：既安全，又不需要每次调用都重新创建。
    返回 None 表示 zstandard 不可用。
    """
    ctx = getattr(_ZSTD_LOCAL, 'ctx', None)
    if ctx is None:
        try:
            import zstandard as zstd
            ctx = zstd.ZstdDecompressor()
        except ImportError:
            ctx = False
        _ZSTD_LOCAL.ctx = ctx
    return ctx or None


def decompress_content(raw_content: bytes) -> bytes:
    """Decompress WeChat 4.x zstd-compressed message content.

    Checks for the zstd frame magic bytes (\\x28\\xb5\\x2f\\xfd) and
    decompresses if present. Passes through non-compressed content unchanged.

    Args:
        raw_content: Raw message content bytes, or None.

    Returns:
        Decompressed bytes if input had zstd magic and decompression succeeded,
        original bytes if no zstd magic was detected,
        or None if input is None or decompression failed.
    """
    if raw_content is None:
        return None
    if len(raw_content) < 4 or raw_content[:4] != _ZSTD_MAGIC:
        return raw_content
    ctx = get_zstd_decompressor()
    if ctx is None:
        return None
    try:
        return ctx.decompress(raw_content, max_output_size=50 * 1024 * 1024)
    except Exception:
        return None


def split_sender_prefix(raw: str, is_group: bool, is_sender: bool) -> tuple:
    """Split sender prefix from group message content.

    For group messages where the current user is not the sender, extracts
    the sender's wxid from the "sender:\\ncontent" prefix that WeChat prepends.

    Args:
        raw: The raw message content string.
        is_group: Whether the chat is a group chat.
        is_sender: Whether the current user sent the message.

    Returns:
        Tuple of (sender, body). If a prefix was found, sender is the raw
        sender prefix text and body is the remaining content. Otherwise
        sender is an empty string and body is the full raw string.
    """
    if is_group and not is_sender and raw and ':\n' in raw[:100]:
        parts = raw.split(':\n', 1)
        return (parts[0], parts[1])
    return ('', raw)
