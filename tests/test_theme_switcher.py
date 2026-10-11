# -*- coding: utf-8 -*-
"""Theme switcher assets and base template wiring."""
from __future__ import annotations

import os
import re

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
BASE = os.path.join(ROOT, 'src', 'web', 'templates', 'base.html')
THEMES_CSS = os.path.join(ROOT, 'src', 'web', 'static', 'css', 'themes.css')
THEME_JS = os.path.join(ROOT, 'src', 'web', 'static', 'js', 'theme.js')

EXPECTED = ('pixel', 'youth', 'tech', 'anime', 'wechat', 'qq')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_theme_files_exist():
    assert os.path.isfile(THEMES_CSS)
    assert os.path.isfile(THEME_JS)
    assert os.path.isfile(BASE)


def test_base_has_theme_picker_and_early_script():
    html = _read(BASE)
    assert 'wechat-export-theme' in html
    assert 'theme-picker' in html
    assert 'css/themes.css' in html
    assert 'js/theme.js' in html
    assert 'WeChat Export' in html
    for tid in EXPECTED:
        assert f'data-theme-set="{tid}"' in html


def test_themes_css_defines_all_packs():
    css = _read(THEMES_CSS)
    for tid in EXPECTED:
        assert f'html[data-theme="{tid}"]' in css


def test_theme_js_lists_and_persists():
    js = _read(THEME_JS)
    assert "STORAGE_KEY = 'wechat-export-theme'" in js
    assert "DEFAULT_THEME = 'pixel'" in js
    for tid in EXPECTED:
        assert f"id: '{tid}'" in js
    assert 'localStorage.setItem' in js
    assert 'data-theme' in js


def test_app_css_exposes_theme_tokens():
    css = _read(os.path.join(ROOT, 'src', 'web', 'static', 'css', 'app.css'))
    for token in ('--radius', '--elev', '--bubble-me-bg', '--bg-overlay', '--font-brand'):
        assert token in css
    # default root should not hard-require a single fixed pink gradient only
    assert re.search(r'--bg-overlay\s*:', css)
