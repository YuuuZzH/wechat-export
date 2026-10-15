# -*- coding: utf-8 -*-
"""Media integrity API — missing media inventory + one-click remigrate."""
import os
import sys
import threading

from flask import Blueprint, request, jsonify, current_app

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from web.sse import create_sse_progress, sse_response

media_integrity_bp = Blueprint('media_integrity', __name__, url_prefix='/api/media')


def _safe_path(user_input: str) -> str:
    return os.path.realpath(os.path.abspath(user_input))


def _cfg():
    decrypted = current_app.config.get('DECRYPTED_DIR', '')
    db_dir = current_app.config.get('DB_DIR') or ''
    if not db_dir:
        try:
            from engine.config_file import get_db_dir
            db_dir = get_db_dir() or ''
        except Exception:
            db_dir = ''
    if not decrypted:
        try:
            from engine.config_file import get_backup_data_dir
            decrypted = get_backup_data_dir() or ''
        except Exception:
            decrypted = ''
    return decrypted, db_dir


def _parse_types(raw) -> list:
    if raw is None or raw == '':
        return [3, 43, 34]
    if isinstance(raw, list):
        out = []
        for x in raw:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                pass
        return out or [3, 43, 34]
    parts = str(raw).replace(';', ',').split(',')
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except ValueError:
            pass
    return out or [3, 43, 34]


@media_integrity_bp.route('/integrity', methods=['GET', 'POST'])
def media_integrity_scan():
    """GET|POST /api/media/integrity — Scan backup for missing media."""
    from backup.media_integrity import scan_integrity

    data = request.get_json(silent=True) or {}
    decrypted, db_dir = _cfg()
    output_dir = data.get('output_dir') or request.args.get('output_dir') or decrypted
    user_db = data.get('db_dir') or request.args.get('db_dir') or db_dir
    if output_dir:
        output_dir = _safe_path(output_dir)
    if user_db:
        user_db = _safe_path(user_db)
    if not output_dir or not os.path.isdir(os.path.join(output_dir, 'message')):
        return jsonify({
            'ok': False,
            'error': '未找到有效备份目录。请先完成一键备份，或在设置中指定工作集。',
            'summary': {},
            'missing': [],
        }), 400

    start_date = data.get('start_date') or request.args.get('start_date') or None
    end_date = data.get('end_date') or request.args.get('end_date') or None
    types = _parse_types(data.get('types') if 'types' in data else request.args.get('types'))
    try:
        limit = int(data.get('limit') or request.args.get('limit') or 500)
    except (TypeError, ValueError):
        limit = 500

    result = scan_integrity(
        output_dir,
        user_db or None,
        start_date=start_date or None,
        end_date=end_date or None,
        types=types,
        limit_items=limit,
    )
    status = 200 if result.get('ok') else 400
    return jsonify(result), status


@media_integrity_bp.route('/integrity/remigrate', methods=['POST'])
def media_integrity_remigrate():
    """POST /api/media/integrity/remigrate — SSE one-click fill missing media."""
    from backup.media_integrity import remigrate_missing

    data = request.get_json(silent=True) or {}
    decrypted, db_dir = _cfg()
    output_dir = data.get('output_dir') or decrypted
    user_db = data.get('db_dir') or db_dir
    if output_dir:
        output_dir = _safe_path(output_dir)
    if user_db:
        user_db = _safe_path(user_db)
    if not output_dir or not os.path.isdir(os.path.join(output_dir, 'message')):
        push, gen = create_sse_progress()
        push.error('未找到有效备份目录')
        return sse_response(gen)

    start_date = data.get('start_date') or None
    end_date = data.get('end_date') or None
    types = _parse_types(data.get('types'))

    push, gen = create_sse_progress()

    def _run():
        def _progress(stage, detail, progress):
            push(stage, detail, progress)

        try:
            result = remigrate_missing(
                output_dir,
                user_db or None,
                start_date=start_date,
                end_date=end_date,
                types=types,
                on_progress=_progress,
            )
            push.done(result)
        except Exception as e:
            import traceback
            push.error(f'{e}\n{traceback.format_exc()}')

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)
