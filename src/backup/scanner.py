"""Discover WeChat data directories, accounts, and chats for backup."""
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChatInfo:
    chat_id: str           # wxid or chatroom id
    display_name: str      # resolved display name
    table_name: Optional[str] = None   # Msg_<hash> table name (populated by indexer)
    db_path: Optional[str] = None      # path to message_N.db (populated by indexer)
    message_count: int = 0
    is_group: bool = False
    member_count: int = 0


@dataclass
class AccountInfo:
    wxid: str
    db_storage_path: str   # <root>/xwechat_files/<wxid>/db_storage
    chats: list = field(default_factory=list)


_WXID_RE = re.compile(r'^[a-zA-Z0-9_\-@]{5,64}$')


def scan_accounts(db_storage_path: str) -> AccountInfo:
    """Discover all accounts at a given db_storage path.

    Path expected: <root>/xwechat_files/<wxid>/db_storage
    """
    parent = os.path.dirname(db_storage_path)
    wxid = os.path.basename(parent)
    if not wxid or not _WXID_RE.match(wxid):
        raise ValueError(
            f"Cannot extract valid wxid from path '{db_storage_path}'. "
            f"Expected path like '<root>/xwechat_files/<wxid>/db_storage'"
        )
    return AccountInfo(wxid=wxid, db_storage_path=db_storage_path)


def scan_chats(decrypted_dir: str, wxid: str = None) -> list:
    """Enumerate all chats from decrypted message databases.

    Walks message/ directory, reads Msg_* tables, resolves display names
    from contact.db and session.db. Reuses logic from engine.services.chat.
    """
    from engine.services.chat import get_contacts
    contacts = get_contacts(decrypted_dir, wxid)
    results = []
    for c in contacts:
        results.append(ChatInfo(
            chat_id=c['id'],
            display_name=c['name'],
            message_count=c.get('message_count', 0),
            is_group=c['id'].endswith('@chatroom'),
            member_count=c.get('member_count', 0),
        ))
    return results


def detect_wechat_installations() -> list:
    """Return list of db_storage paths for all WeChat installations found."""
    from engine.utils import find_all_wechat_data_dirs
    entries = find_all_wechat_data_dirs()
    return [e['db_path'] for e in entries]
