# -*- coding: utf-8 -*-
"""In-app docs page is rendered from README.md."""
from __future__ import annotations

import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'src'))


def test_render_readme_contains_brand_and_disclaimer():
    from web.app import _render_readme_html
    html = _render_readme_html()
    assert 'WeChat Export' in html
    assert '免责声明' in html
    assert 'WeChat Report' not in html


def test_docs_template_exists_manual_removed():
    assert os.path.isfile(os.path.join(ROOT, 'src', 'web', 'templates', 'docs.html'))
    assert not os.path.isfile(os.path.join(ROOT, 'src', 'web', 'templates', 'manual.html'))


def test_base_nav_points_to_docs():
    base = open(os.path.join(ROOT, 'src', 'web', 'templates', 'base.html'), encoding='utf-8').read()
    assert 'href="/docs"' in base
    assert '使用手册' not in base
    assert '>手册<' not in base
