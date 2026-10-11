# -*- coding: utf-8 -*-
"""Blur / cover PII regions in README screenshots (wxid paths, chat names, etc.)."""
from __future__ import annotations

import os
import re
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SHOT = os.path.join(ROOT, 'screenshots')


def _font(size: int):
    for name in (
        r'C:\Windows\Fonts\msyh.ttc',
        r'C:\Windows\Fonts\simhei.ttf',
        r'C:\Windows\Fonts\arial.ttf',
    ):
        if os.path.isfile(name):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass
    return ImageFont.load_default()


def cover(im: Image.Image, box, fill=(255, 236, 242), label: str | None = None):
    """Solid cover + optional centered label."""
    d = ImageDraw.Draw(im)
    d.rectangle(box, fill=fill)
    if label:
        font = _font(14)
        x0, y0, x1, y1 = box
        tw = d.textlength(label, font=font) if hasattr(d, 'textlength') else len(label) * 8
        tx = x0 + max(4, (x1 - x0 - tw) / 2)
        ty = y0 + max(2, (y1 - y0 - 18) / 2)
        d.text((tx, ty), label, fill=(120, 80, 100), font=font)


def blur(im: Image.Image, box, radius=8):
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = im.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(radius))
    im.paste(crop, (x0, y0))


def redact_chat_list_sidebar(im: Image.Image):
    """Left contact list name column (chat viewer / cleanup)."""
    w, h = im.size
    # name text area roughly after avatar
    cover(im, (70, 100, int(w * 0.28), h - 20), label='')
    # paint fake rows
    d = ImageDraw.Draw(im)
    font = _font(13)
    names = [
        ('示例群聊 A', '群'),
        ('示例群聊 B', '群'),
        ('联系人甲', '人'),
        ('联系人乙', '人'),
        ('示例群聊 C', '群'),
        ('文件传输助手', '人'),
        ('联系人丙', '人'),
        ('示例群聊 D', '群'),
    ]
    y = 118
    for name, kind in names:
        d.text((78, y), name, fill=(30, 30, 30), font=font)
        y += 52


def redact_chat_header(im: Image.Image):
    w, h = im.size
    # selected chat title in main pane
    cover(im, (int(w * 0.30) + 50, 70, int(w * 0.70), 100), label='示例群聊 A')


def redact_message_senders(im: Image.Image):
    w, h = im.size
    # sender names in message stream (left of bubbles)
    for y in range(200, min(h - 80, 700), 70):
        cover(im, (int(w * 0.32) + 40, y, int(w * 0.32) + 120, y + 20), label='成员')


def redact_message_bodies(im: Image.Image):
    w, h = im.size
    # cover bubble text areas with placeholder
    cover(im, (int(w * 0.32) + 40, 230, w - 40, h - 90), label='（演示消息内容已打码）')


def redact_export_chat_names(im: Image.Image):
    w, h = im.size
    # chat picker rows name column
    cover(im, (60, 230, int(w * 0.55), min(h - 180, 520)), label='')
    d = ImageDraw.Draw(im)
    font = _font(14)
    demos = [
        '示例群聊 A', '联系人甲', '示例群聊 B', '示例群聊 C',
        '联系人乙', '示例群聊 D',
    ]
    y = 245
    for name in demos:
        d.text((90, y), name, fill=(30, 30, 30), font=font)
        y += 42


def redact_path_fields(im: Image.Image):
    """Cover any wide input that likely holds absolute paths / wxid."""
    w, h = im.size
    # common path dropdown / input bands under forms
    # Cover mid-page wide fields (heuristic bands)
    bands = [
        (40, 150, w - 200, 190),
        (40, 195, w - 40, 235),
        (40, 240, int(w * 0.7), 280),
    ]
    for box in bands:
        # only cover if region is fairly light (input-like) — always cover conservatively
        cover(im, box, fill=(255, 255, 255), label=r'D:\WeChat\xwechat_files\wxid_demo\db_storage')


def process(name: str, fns):
    path = os.path.join(SHOT, name)
    if not os.path.isfile(path):
        print('skip missing', name)
        return
    im = Image.open(path).convert('RGB')
    for fn in fns:
        fn(im)
    im.save(path, optimize=True)
    print('redacted', name, im.size)


def main():
    # Chat viewer family
    for n in (
        '05-chat-viewer.png',
        '11-chat-viewer-layout.png',
        '12-filter-panel.png',
        '13-message-types.png',
        '14-voice-transcribe.png',
    ):
        process(n, [
            redact_chat_list_sidebar,
            redact_chat_header,
            redact_message_senders,
            redact_message_bodies,
        ])

    # Export
    for n in ('08-export-page.png', '09-export-select.png'):
        process(n, [redact_export_chat_names])

    # Cleanup
    for n in ('16-cleanup-list.png', '17-cleanup-detail.png'):
        process(n, [redact_chat_list_sidebar])

    # Paths / wxid
    for n in (
        '03-backup-page.png',
        '04-backup-progress.png',
        '20-manual-keys.png',
        '21-settings-page.png',
    ):
        process(n, [redact_path_fields])

    print('done')


if __name__ == '__main__':
    main()
