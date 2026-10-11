"""Regression: export must not KeyError on 'table_name' from fast_chat_list."""
import hashlib
import sqlite3
from pathlib import Path

from chat_export import _normalize_tables, collect_messages
from chatlab_pull_server import fast_chat_list


def test_normalize_tables_fills_missing_table_name():
    uname = '123@chatroom'
    tname = 'Msg_%s' % hashlib.md5(uname.encode()).hexdigest()
    chat = {
        'username': uname,
        'tables': [{'db_path': r'D:\fake\message_0.db'}],  # old fast_chat_list shape
    }
    tables = _normalize_tables(chat)
    assert len(tables) == 1
    assert tables[0]['table_name'] == tname


def test_normalize_tables_keeps_existing():
    chat = {
        'username': 'u',
        'tables': [{'db_path': 'x.db', 'table_name': 'Msg_abc'}],
    }
    assert _normalize_tables(chat)[0]['table_name'] == 'Msg_abc'


def test_fast_chat_list_includes_table_name(tmp_path):
    dec = tmp_path / 'dec'
    msg = dec / 'message'
    msg.mkdir(parents=True)
    (dec / 'contact').mkdir()
    # contact.db (optional for names)
    cdb = sqlite3.connect(dec / 'contact' / 'contact.db')
    cdb.execute('CREATE TABLE contact (id INTEGER, username TEXT, remark TEXT,'
                ' nick_name TEXT, alias TEXT)')
    cdb.execute("INSERT INTO contact VALUES (1,'g@chatroom','导出1群','','')")
    cdb.commit()
    cdb.close()

    uname = 'g@chatroom'
    tname = 'Msg_%s' % hashlib.md5(uname.encode()).hexdigest()
    db = sqlite3.connect(msg / 'message_0.db')
    db.execute('CREATE TABLE Name2Id (user_name TEXT)')
    db.execute('INSERT INTO Name2Id VALUES (?)', (uname,))
    db.execute('CREATE TABLE [%s] (local_id INTEGER, local_type INTEGER,'
               ' real_sender_id INTEGER, create_time INTEGER, status INTEGER,'
               ' message_content TEXT, origin_source INTEGER)' % tname)
    db.execute(
        'INSERT INTO [%s] VALUES (1,1,1,1700000001,0,?,2)' % tname,
        ('hello 号',))
    db.commit()
    db.close()

    chats = fast_chat_list(str(dec), with_counts=True)
    assert len(chats) == 1
    assert chats[0]['username'] == uname
    assert chats[0]['msg_count'] == 1
    assert chats[0]['tables'][0]['table_name'] == tname

    # collect_messages must not raise KeyError
    msgs = collect_messages(chats[0], keyword='号')
    assert len(msgs) == 1
    assert '号' in msgs[0]['text']


def test_keyword_matches_content_not_sender_name(tmp_path):
    """Nickname containing the keyword must NOT pull in unrelated messages."""
    import hashlib
    import sqlite3
    from chat_export import collect_messages

    uname = 'g@chatroom'
    tname = 'Msg_%s' % hashlib.md5(uname.encode()).hexdigest()
    dec = tmp_path / 'dec'
    msg = dec / 'message'
    msg.mkdir(parents=True)
    (dec / 'contact').mkdir()
    cdb = sqlite3.connect(dec / 'contact' / 'contact.db')
    cdb.execute('CREATE TABLE contact (id INTEGER, username TEXT, remark TEXT,'
                ' nick_name TEXT, alias TEXT)')
    cdb.execute("INSERT INTO contact VALUES (1,'wxid_a','洲收号','','')")
    cdb.commit()
    cdb.close()

    dbp = msg / 'message_0.db'
    db = sqlite3.connect(dbp)
    db.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    db.execute("INSERT INTO Name2Id VALUES (1, ?)", (uname,))
    db.execute("INSERT INTO Name2Id VALUES (2, 'wxid_a')")
    db.execute(
        'CREATE TABLE [%s] (local_id INTEGER, local_type INTEGER,'
        ' real_sender_id INTEGER, create_time INTEGER, status INTEGER,'
        ' message_content TEXT, origin_source INTEGER)' % tname)
    # sender nickname has 号, but body does not
    db.execute(
        'INSERT INTO [%s] VALUES (1,1,2,1700000001,0,?,2)' % tname,
        ('刚醒',))
    # body contains 号
    db.execute(
        'INSERT INTO [%s] VALUES (2,1,2,1700000002,0,?,2)' % tname,
        ('这个号很好',))
    db.commit()
    db.close()

    chat = {
        'username': uname,
        'display_name': '导出1群',
        'tables': [{'db_path': str(dbp), 'table_name': tname}],
    }
    msgs = collect_messages(chat, keyword='号')
    assert len(msgs) == 1
    assert msgs[0]['text'] == '这个号很好'
