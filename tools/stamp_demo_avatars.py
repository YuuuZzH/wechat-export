# -*- coding: utf-8 -*-
"""Paste a shared demo avatar onto chat-list / bubble avatar slots in screenshots."""
from __future__ import annotations

import os

from PIL import Image

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SHOT = os.path.join(ROOT, 'screenshots')
AVATAR = os.path.join(SHOT, '_demo-avatar.png')

TARGETS = [
    '05-chat-viewer-masked.png',
    '11-chat-viewer-layout-masked.png',
    '12-filter-panel-masked.png',
    '13-message-types-masked.png',
    '14-voice-transcribe-masked.png',
]


def stamp(path: str, avatar: Image.Image):
    im = Image.open(path).convert('RGBA')
    w, h = im.size
    side = avatar.resize((36, 36), Image.Resampling.NEAREST)

    # left contact list avatars
    for y in range(108, h - 40, 52):
        im.paste(side, (16, y), side if side.mode == 'RGBA' else None)

    # chat header avatar
    hx = int(w * 0.30) + 10
    im.paste(side, (hx, 70), side if side.mode == 'RGBA' else None)

    # message-row avatars
    mx = int(w * 0.30) + 14
    for y in range(205, h - 95, 70):
        im.paste(side, (mx, y), side if side.mode == 'RGBA' else None)

    out = im.convert('RGB')
    out.save(path, optimize=True)
    print('stamped', os.path.basename(path))


def main():
    avatar = Image.open(AVATAR).convert('RGBA')
    for name in TARGETS:
        path = os.path.join(SHOT, name)
        if os.path.isfile(path):
            stamp(path, avatar)
        else:
            print('missing', name)


if __name__ == '__main__':
    main()
