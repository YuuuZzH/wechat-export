"""Cleanup API endpoints."""
import os
import sys
from flask import Blueprint, request, jsonify, current_app

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

cleanup_bp = Blueprint('cleanup', __name__)


def _cfg():
    decrypted = current_app.config.get('DECRYPTED_DIR', '')
    wxid = current_app.config.get('WXID')
    db_dir = current_app.config.get('DB_DIR')
    if not db_dir:
        from engine.config_file import get_db_dir
        db_dir = get_db_dir()
    if not db_dir:
        from engine.utils import find_all_wechat_data_dirs
        dirs = find_all_wechat_data_dirs()
        if dirs:
            db_dir = dirs[0].get('db_path', '')
    return (decrypted, wxid, db_dir)


@cleanup_bp.route('/cleanup/analyze')
def cleanup_analyze():
    """Return space usage analysis for all chats, sorted by size DESC."""
    decrypted_dir, wxid, db_dir = _cfg()
    from engine.services.cleanup import analyze_chats
    try:
        result = analyze_chats(decrypted_dir, db_dir)
        chats = result['chats']
        sampling = result.get('sampling', {})
        total_bytes = sum(c['total_bytes'] for c in chats)
        return jsonify({'chats': chats, 'total_bytes': total_bytes, 'sampling': sampling})
    except Exception as e:
        return jsonify({'error': str(e), 'chats': [], 'total_bytes': 0, 'sampling': {}}), 500


@cleanup_bp.route('/cleanup/preview', methods=['POST'])
def cleanup_preview():
    """Preview deletion scope. Returns confirm_token valid for 60s."""
    decrypted_dir, wxid, db_dir = _cfg()
    body = request.get_json(silent=True) or {}
    chat_ids = body.get('chat_ids', [])
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    msg_types = body.get('msg_types', [3, 6, 43])

    if not chat_ids:
        return jsonify({'error': '请选择至少一个聊天'}), 400

    from engine.services.cleanup import preview_deletion
    try:
        result = preview_deletion(
            decrypted_dir, db_dir, chat_ids,
            start_date, end_date, msg_types
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@cleanup_bp.route('/cleanup/execute', methods=['POST'])
def cleanup_execute():
    """Execute deletion. Requires confirm_token from /preview."""
    decrypted_dir, wxid, db_dir = _cfg()
    body = request.get_json(silent=True) or {}
    confirm_token = body.get('confirm_token', '')
    chat_ids = body.get('chat_ids', [])
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    msg_types = body.get('msg_types', [3, 6, 43])

    if not confirm_token:
        return jsonify({'error': '缺少确认令牌'}), 400

    from engine.services.cleanup import execute_deletion
    try:
        result = execute_deletion(
            decrypted_dir, db_dir, confirm_token,
            chat_ids, start_date, end_date, msg_types,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e), 'deleted_files': 0,
                        'freed_bytes': 0}), 500
