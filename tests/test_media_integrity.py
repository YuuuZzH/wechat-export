# -*- coding: utf-8 -*-
"""Media integrity scan + remigrate (synthetic fixtures)."""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from backup.media_integrity import remigrate_missing, scan_integrity  # noqa: E402
from backup.migrator import migrate_media  # noqa: E402


def _msg_db_with_refs(path: str, chat: str, items: list):
    """Create a message_*.db with Name2Id + Msg_<md5> and zstd XML content.

    items: list of (local_id, local_type, create_time, md5_or_none)
    For type 34, md5_or_none is ignored (voice).
    """
    import hashlib
    import zstandard as zstd

    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = hashlib.md5(chat.encode()).hexdigest()
    tname = f'Msg_{h}'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE Name2Id (user_name TEXT)')
    conn.execute('INSERT INTO Name2Id(user_name) VALUES (?)', (chat,))
    conn.execute(
        f'CREATE TABLE [{tname}] ('
        'local_id INT, local_type INT, create_time INT, message_content BLOB)'
    )
    cctx = zstd.ZstdCompressor()
    for local_id, ltype, cts, md5 in items:
        if ltype == 34:
            blob = cctx.compress(b'<msg></msg>')
        else:
            xml = f'<msg><img md5="{md5}"/></msg>'.encode()
            blob = cctx.compress(xml)
        conn.execute(
            f'INSERT INTO [{tname}] VALUES (?,?,?,?)',
            (local_id, ltype, cts, blob),
        )
    conn.commit()
    conn.close()


def _voice_db(path: str, create_time: int, local_id: int, payload: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE VoiceInfo (create_time INT, local_id INT, voice_data BLOB)'
    )
    conn.execute(
        'INSERT INTO VoiceInfo VALUES (?,?,?)',
        (create_time, local_id, payload),
    )
    conn.commit()
    conn.close()


def test_scan_detects_missing_and_present(tmp_path):
    backup = tmp_path / 'backup'
    msg = backup / 'message'
    media = backup / 'media' / 'images'
    media.mkdir(parents=True)
    md5_ok = 'a' * 32
    md5_miss = 'b' * 32
    _msg_db_with_refs(
        str(msg / 'message_0.db'),
        'wxid_alice',
        [
            (1, 3, 1700000001, md5_ok),
            (2, 3, 1700000002, md5_miss),
        ],
    )
    (media / f'{md5_ok}.dat').write_bytes(b'img')

    result = scan_integrity(str(backup), None, types=[3], limit_items=50)
    assert result['ok']
    s = result['summary']
    assert s['total'] == 2
    assert s['present'] == 1
    assert s['missing'] == 1
    assert result['missing'][0]['md5'] == md5_miss


def test_remigrate_from_prior_backup(tmp_path):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    md5 = 'c' * 32
    (old / 'media' / 'images').mkdir(parents=True)
    (old / 'message').mkdir(parents=True)
    (old / 'media' / 'images' / f'{md5}.dat').write_bytes(b'from-old')
    (new / 'message').mkdir(parents=True)
    (new / 'media' / 'images').mkdir(parents=True)
    _msg_db_with_refs(
        str(new / 'message' / 'message_0.db'),
        'wxid_bob',
        [(1, 3, 1700000100, md5)],
    )

    before = scan_integrity(str(new), None, types=[3], link_dest=str(old))
    assert before['summary']['missing'] == 1
    assert before['summary']['recoverable'] == 1

    out = remigrate_missing(
        str(new), None, types=[3], link_dest=str(old),
    )
    assert out['ok']
    assert (new / 'media' / 'images' / f'{md5}.dat').is_file()
    assert (new / 'media' / 'images' / f'{md5}.dat').read_bytes() == b'from-old'


def test_voice_extract_on_remigrate(tmp_path):
    backup = tmp_path / 'backup'
    (backup / 'message').mkdir(parents=True)
    _msg_db_with_refs(
        str(backup / 'message' / 'message_0.db'),
        'wxid_carol',
        [(9, 34, 1700000200, None)],
    )
    _voice_db(
        str(backup / 'message' / 'media_0.db'),
        1700000200, 9, b'SILK_V3' + b'\x00' * 20,
    )

    scan = scan_integrity(str(backup), None, types=[34])
    assert scan['summary']['missing'] == 1
    assert scan['summary']['recoverable'] == 1

    out = remigrate_missing(str(backup), None, types=[34])
    assert out['ok']
    assert out['voice']['extracted'] == 1
    silk = backup / 'media' / 'voice' / '1700000200_9.silk'
    assert silk.is_file()


def test_migrate_media_allowlist(tmp_path):
    """md5_allowlist only processes listed hashes."""
    src_root = tmp_path / 'wx'
    db_storage = src_root / 'db_storage'
    attach = src_root / 'msg' / 'attach' / 'h1' / '2024-01' / 'Img'
    attach.mkdir(parents=True)
    md5 = 'd' * 32
    src_file = attach / f'{md5}.dat'
    src_file.write_bytes(b'x')

    # Minimal hardlink db
    hl = db_storage / 'hardlink'
    hl.mkdir(parents=True)
    conn = sqlite3.connect(str(hl / 'hardlink.db'))
    conn.execute(
        'CREATE TABLE image_hardlink_info_v4 '
        '(md5 TEXT, file_name TEXT, dir1 INT, dir2 INT)'
    )
    conn.execute('CREATE TABLE dir2id (username TEXT)')
    conn.execute('INSERT INTO dir2id(rowid, username) VALUES (1, ?)', ('h1',))
    conn.execute('INSERT INTO dir2id(rowid, username) VALUES (2, ?)', ('2024-01',))
    # sqlite rowid: insert without rowid control — use explicit
    conn.execute('DELETE FROM dir2id')
    conn.execute('INSERT INTO dir2id(username) VALUES (?)', ('h1',))
    d1 = conn.execute('SELECT rowid FROM dir2id WHERE username=?', ('h1',)).fetchone()[0]
    conn.execute('INSERT INTO dir2id(username) VALUES (?)', ('2024-01',))
    d2 = conn.execute('SELECT rowid FROM dir2id WHERE username=?', ('2024-01',)).fetchone()[0]
    conn.execute(
        'INSERT INTO image_hardlink_info_v4(md5, file_name, dir1, dir2) VALUES (?,?,?,?)',
        (md5, f'{md5}.dat', d1, d2),
    )
    conn.commit()
    conn.close()

    out = tmp_path / 'out'
    (out / 'message').mkdir(parents=True)
    stats = migrate_media(
        str(db_storage), str(out),
        refs=[{'md5': md5, 'media_type': 3}, {'md5': 'e' * 32, 'media_type': 3}],
        md5_allowlist={md5},
    )
    assert stats['total'] == 1
    assert (out / 'media' / 'images' / f'{md5}.dat').is_file() or stats['hardlinked'] + stats['copied'] >= 1


def test_api_page_and_scan(tmp_path):
    from web.app import create_app

    backup = tmp_path / 'backup'
    (backup / 'message').mkdir(parents=True)
    _msg_db_with_refs(
        str(backup / 'message' / 'message_0.db'),
        'wxid_dave',
        [(1, 3, 1700000300, 'f' * 32)],
    )
    app = create_app(decrypted_dir=str(backup))
    cli = app.test_client()
    r = cli.get('/media-integrity')
    assert r.status_code == 200
    assert b'missing-table' in r.data
    r = cli.post('/api/media/integrity', json={'types': [3]})
    j = r.get_json()
    assert j['ok']
    assert j['summary']['missing'] >= 1
