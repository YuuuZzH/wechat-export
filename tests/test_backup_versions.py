# -*- coding: utf-8 -*-
"""Backup version registry: register / list / diff / activate."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), '..', 'src')
if ROOT not in sys.path:
    sys.path.insert(0, os.path.abspath(ROOT))

from backup.versions import (  # noqa: E402
    activate_version,
    default_version_output_dir,
    diff_versions,
    find_previous_version,
    list_versions,
    register_version,
    version_stamp,
)


def _make_backup(root: str, name: str, chats: list[dict]) -> str:
    path = os.path.join(root, name)
    os.makedirs(os.path.join(path, 'message'), exist_ok=True)
    data = os.path.join(path, 'data')
    os.makedirs(data, exist_ok=True)
    db = os.path.join(data, 'chats.db')
    conn = sqlite3.connect(db)
    conn.execute(
        'CREATE TABLE chats ('
        'chat_id TEXT, display_name TEXT, message_count INT, '
        'last_msg_time INT, is_group INT)'
    )
    for c in chats:
        conn.execute(
            'INSERT INTO chats VALUES (?,?,?,?,?)',
            (
                c['chat_id'],
                c.get('display_name') or c['chat_id'],
                int(c.get('message_count') or 0),
                c.get('last_msg_time'),
                1 if c.get('is_group') else 0,
            ),
        )
    conn.commit()
    conn.close()
    return path


class BackupVersionsTest(unittest.TestCase):
    def test_stamp_and_default_dir(self):
        stamp = version_stamp()
        self.assertRegex(stamp, r'^\d{4}-\d{2}-\d{2}_\d{6}$')
        out = default_version_output_dir(os.path.join('D:', 'backup'))
        self.assertTrue(out.endswith(stamp) or stamp in out)
        self.assertIn('backup', out)

    def test_register_list_diff_activate(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = _make_backup(tmp, '2026-01-01_120000', [
                {'chat_id': 'wxid_a', 'display_name': 'Alice', 'message_count': 10},
                {'chat_id': 'room@chatroom', 'display_name': '群', 'message_count': 5, 'is_group': True},
            ])
            new = _make_backup(tmp, '2026-01-02_130000', [
                {'chat_id': 'wxid_a', 'display_name': 'Alice', 'message_count': 15},
                {'chat_id': 'wxid_b', 'display_name': 'Bob', 'message_count': 3},
                {'chat_id': 'room@chatroom', 'display_name': '群', 'message_count': 5, 'is_group': True},
            ])
            m1 = register_version(old, wxid='wxid_me')
            m2 = register_version(new, wxid='wxid_me', parent_path=old)
            # Stable chronological order for list/find_previous
            for meta_path, ts in (
                (os.path.join(old, 'data', 'version_meta.json'), 1700000000),
                (os.path.join(new, 'data', 'version_meta.json'), 1700001000),
            ):
                with open(meta_path, encoding='utf-8') as f:
                    meta = json.load(f)
                meta['created_at'] = ts
                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump(meta, f)
            self.assertEqual(m1['chat_count'], 2)
            self.assertEqual(m2['chat_count'], 3)
            self.assertTrue(os.path.isfile(os.path.join(new, 'data', 'version_meta.json')))
            self.assertTrue(os.path.isfile(os.path.join(new, 'data', 'chats_snapshot.json')))

            listed = list_versions(tmp, limit=10)
            self.assertGreaterEqual(len(listed), 2)
            self.assertEqual(listed[0]['id'], '2026-01-02_130000')

            prev = find_previous_version(new)
            self.assertEqual(os.path.abspath(prev), os.path.abspath(old))

            diff = diff_versions(old, new)
            self.assertTrue(diff['ok'])
            self.assertEqual(diff['summary']['added_chats'], 1)
            self.assertEqual(diff['summary']['message_delta'], 8)  # +5 Alice +3 Bob
            added_ids = {c['chat_id'] for c in diff['added']}
            self.assertIn('wxid_b', added_ids)
            grown = {c['chat_id']: c['delta'] for c in diff['grown']}
            self.assertEqual(grown.get('wxid_a'), 5)

            # activate writes config — use isolated config path via monkeypatch
            import engine.config_file as cf
            cfg_path = os.path.join(tmp, 'cfg.json')
            old_fn = cf._config_path
            cf._config_path = lambda: cfg_path
            try:
                act = activate_version(old, wxid='wxid_me')
                self.assertTrue(act['ok'])
                with open(cfg_path, encoding='utf-8') as f:
                    cfg = json.load(f)
                self.assertEqual(os.path.abspath(cfg['last_backup_data_dir']), os.path.abspath(old))
            finally:
                cf._config_path = old_fn


if __name__ == '__main__':
    unittest.main()
