# -*- coding: utf-8 -*-
"""Offline chart assets — copy vendored ECharts next to generated HTML."""
import os
import shutil
import sys


def vendor_dir():
    """Locate web/static/vendor (dev tree or PyInstaller bundle)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, 'web', 'static', 'vendor'),
    ]
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', '') or ''
        if meipass:
            candidates.insert(0, os.path.join(meipass, 'web', 'static', 'vendor'))
            candidates.insert(0, os.path.join(meipass, 'static', 'vendor'))
        candidates.insert(0, os.path.join(os.path.dirname(sys.executable),
                                          'web', 'static', 'vendor'))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


def materialize_script_tags(out_path, filenames):
    """Copy vendor JS beside out_path and return relative <script src> tags.

    Falls back to /static/vendor/… (served by Flask) then CDN if missing.
    """
    vdir = vendor_dir()
    out_dir = os.path.dirname(os.path.abspath(out_path)) if out_path else ''
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tags = []
    for name in filenames:
        src = os.path.join(vdir, name)
        if out_dir and os.path.isfile(src):
            dest = os.path.join(out_dir, name)
            try:
                if (not os.path.isfile(dest)
                        or os.path.getsize(dest) != os.path.getsize(src)):
                    shutil.copy2(src, dest)
                tags.append('<script src="%s"></script>' % name)
                continue
            except OSError:
                pass
        if os.path.isfile(src):
            tags.append('<script src="/static/vendor/%s"></script>' % name)
        else:
            # Last resort so old installs still render online
            if name == 'echarts.min.js':
                tags.append(
                    '<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/'
                    'dist/echarts.min.js"></script>')
            elif name == 'echarts-wordcloud.min.js':
                tags.append(
                    '<script src="https://cdn.jsdelivr.net/npm/'
                    'echarts-wordcloud@2.1.0/dist/echarts-wordcloud.min.js">'
                    '</script>')
    return '\n'.join(tags)


def echarts_tags(out_path, wordcloud=False):
    names = ['echarts.min.js']
    if wordcloud:
        names.append('echarts-wordcloud.min.js')
    return materialize_script_tags(out_path, names)
