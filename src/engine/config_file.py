"""Persistent config file for tracking backup output path across sessions."""
import json
import os

CONFIG_FILENAME = ".wechat_exp_config.json"


def _config_path() -> str:
    """Config file lives at project root (dev) or next to the exe (frozen)."""
    import sys as _sys
    if getattr(_sys, 'frozen', False):
        base = os.path.dirname(_sys.executable)
    else:
        # config_file.py → engine/ → src/ → project_root/
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, CONFIG_FILENAME)


def _read_backup_dir_from(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        raw = cfg.get("last_backup_data_dir", "")
        if raw and os.path.isdir(raw):
            return raw
    except (ValueError, OSError):
        pass
    return None


def get_backup_data_dir() -> str | None:
    """Return the output directory from the last successful backup, or None.

    Also checks ``dist/.wechat_exp_config.json`` so source-mode runs can reuse
    a backup that was created by the packaged exe.
    """
    path = _config_path()
    found = _read_backup_dir_from(path)
    if found:
        return found
    # Dev fallback: exe often writes config next to dist\wechat_exp.exe
    import sys as _sys
    if not getattr(_sys, 'frozen', False):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for alt in (
            os.path.join(root, 'dist', CONFIG_FILENAME),
            os.path.join(root, 'dist', 'backup'),  # not a config; handled below
        ):
            if alt.endswith(CONFIG_FILENAME):
                found = _read_backup_dir_from(alt)
                if found:
                    return found
        latest = get_latest_backup_dir(os.path.join(root, 'dist', 'backup'))
        if latest:
            return latest
        latest = get_latest_backup_dir(os.path.join(root, 'backup'))
        if latest:
            return latest
    return None


def set_backup_data_dir(data_dir: str, wxid: str | None = None) -> None:
    """Persist the backup data directory for other features to find."""
    path = _config_path()
    cfg = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
    except (ValueError, OSError):
        pass
    cfg["last_backup_data_dir"] = str(data_dir)
    if wxid:
        cfg["last_backup_wxid"] = str(wxid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_backup_wxid() -> str | None:
    """Return the wxid from the last successful backup, or None."""
    path = _config_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        wxid = cfg.get("last_backup_wxid", "")
        if wxid:
            return str(wxid)
    except (ValueError, OSError):
        pass
    return None


def get_latest_backup_dir(base_dir: str) -> str | None:
    """Scan a base backup directory and return the latest backup dir with message/ subdir."""
    if not os.path.isdir(base_dir):
        return None
    candidates = []
    for name in os.listdir(base_dir):
        full = os.path.join(base_dir, name)
        if not os.path.isdir(full):
            continue
        if os.path.isdir(os.path.join(full, "message")):
            candidates.append((name, full))
    if not candidates:
        return None
    candidates.sort(key=lambda x: os.path.getmtime(x[1]), reverse=True)
    return candidates[0][1]


def _get_all_keys_path() -> str:
    """Legacy all_keys.json path — used for one-time migration."""
    import sys as _sys
    if getattr(_sys, 'frozen', False):
        base = os.path.dirname(_sys.executable)
        return os.path.join(base, 'output', 'all_keys.json')
    else:
        # config_file.py → engine/ → src/ → project_root/
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, 'output', 'all_keys.json')


def _migrate_all_keys(cfg: dict, config_path: str) -> dict:
    """One-time: load keys from legacy output/all_keys.json and persist them in config."""
    legacy_path = _get_all_keys_path()
    if not os.path.isfile(legacy_path):
        return {}
    try:
        with open(legacy_path, 'r', encoding='utf-8') as f:
            legacy = json.load(f)
    except (ValueError, OSError):
        return {}

    keys = {}
    db_dir = ''
    for k, v in legacy.items():
        if k.startswith('_'):
            if k == '_db_dir':
                db_dir = str(v)
            continue
        if isinstance(v, dict):
            hex_key = v.get('enc_key', '')
            if hex_key and len(hex_key) == 64:
                keys[k] = hex_key
        elif isinstance(v, str) and len(v) == 64:
            keys[k] = v

    if not keys:
        return {}

    cfg['db_keys'] = keys
    if db_dir:
        cfg['_db_dir'] = db_dir
    try:
        tmp = config_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path)
    except OSError:
        pass
    return keys


def get_db_keys() -> dict:
    """Return database encryption keys from config.

    Returns dict mapping db_rel_path -> 64-char hex enc_key.
    On first call, migrates keys from legacy output/all_keys.json.
    """
    path = _config_path()
    if not os.path.isfile(path):
        # Try migration before creating empty config
        return _migrate_all_keys({}, path)

    try:
        with open(path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (ValueError, OSError):
        return {}

    keys = cfg.get('db_keys', {})
    if not keys:
        keys = _migrate_all_keys(cfg, path)
    return keys


def get_db_keys_by_salt() -> dict:
    """Return {salt_hex: key_hex} from config (empty dict for older configs).

    salt（每个加密库 page1 的前 16 字节）是**文件级唯一标识**：不同微信账号的同名库
    （如各自都有 ``message\\message_0.db``）相对路径相同但 salt 不同。按 salt 存储才能
    避免「后备份的账号覆盖前一个账号的密钥」（issue #21）。
    """
    path = _config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (ValueError, OSError):
        return {}
    by_salt = cfg.get('db_keys_by_salt', {})
    if not isinstance(by_salt, dict):
        return {}
    return {str(k): str(v) for k, v in by_salt.items() if k and v}


def set_db_keys(keys: dict, db_dir: str = '', salt_keys: dict = None) -> None:
    """Persist database encryption keys in the unified config file.

    Args:
        keys: dict mapping db_rel_path -> 64-char hex enc_key
        db_dir: absolute path to WeChat db_storage directory
        salt_keys: optional dict mapping salt_hex -> 64-char hex enc_key。
            **按 salt 存储**，多账号同名库不会互相覆盖（仅累加，不删除）。
    """
    path = _config_path()
    cfg = {}
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
    except (ValueError, OSError):
        pass
    # Merge with existing keys so cold-shard keys from prior runs are not lost
    existing = cfg.get('db_keys', {})
    existing.update({str(k): str(v) for k, v in keys.items()})
    cfg['db_keys'] = existing
    if salt_keys:
        by_salt = cfg.get('db_keys_by_salt', {})
        if not isinstance(by_salt, dict):
            by_salt = {}
        by_salt.update({str(k): str(v) for k, v in salt_keys.items() if k and v})
        cfg['db_keys_by_salt'] = by_salt
    if db_dir:
        cfg['_db_dir'] = str(db_dir)
        dirs = cfg.get('_db_dirs', [])
        if not isinstance(dirs, list):
            dirs = []
        if str(db_dir) not in dirs:
            dirs.append(str(db_dir))
        cfg['_db_dirs'] = dirs
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def persist_extracted_keys(key_map: dict, salt_to_dbs: dict, db_dir: str = '') -> int:
    """把一次密钥提取的成果落盘：**同时**写 rel 表与 salt 表。

    Args:
        key_map: {salt_hex: key_hex}（提取链路内部本来就是按 salt 组织的）
        salt_to_dbs: {salt_hex: [相对路径, ...]}
        db_dir: 本次提取对应的 db_storage 目录

    Returns: 写入 rel 表的条数。
    """
    rel_map = {}
    for salt_hex, key_hex in (key_map or {}).items():
        for rel in (salt_to_dbs or {}).get(salt_hex, []):
            rel_map[str(rel)] = str(key_hex)
    set_db_keys(rel_map, db_dir=db_dir,
                salt_keys={str(k): str(v) for k, v in (key_map or {}).items() if k and v})
    return len(rel_map)


def remove_db_keys(rels) -> int:
    """Delete stored keys for the given db relative paths.

    Accepts an iterable of relative paths (either separator). Unknown entries
    are ignored. Returns the number of entries actually removed.
    """
    path = _config_path()
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (ValueError, OSError):
        return 0
    keys = cfg.get('db_keys', {})
    if not isinstance(keys, dict) or not keys:
        return 0
    wanted = {str(r).replace('/', '\\') for r in rels}
    wanted |= {str(r) for r in rels}
    removed = 0
    for k in list(keys.keys()):
        if k in wanted or k.replace('/', '\\') in wanted:
            keys.pop(k, None)
            removed += 1
    if removed:
        cfg['db_keys'] = keys
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            return 0
    return removed


def set_db_dir(db_dir: str) -> None:
    """Persist the WeChat db_storage path chosen by the user.

    Other features (backup / decrypt / keyscan) read this back via get_db_dir(),
    so a manually picked directory only has to be entered once.
    """
    path = _config_path()
    cfg = {}
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
    except (ValueError, OSError):
        pass
    cfg['_db_dir'] = str(db_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_asr_settings() -> dict:
    """语音转写相关设置（引擎、本地模型、商业 ASR 凭据等）。"""
    defaults = {
        "engine": "local",              # local | baidu
        "local_model": "Xenova/whisper-base",
        "language": "zh",               # zh | auto | en ...
        "simplify": True,               # 简繁转换（Whisper 常输出繁体）
        "baidu_api_key": "",
        "baidu_secret_key": "",
        "baidu_dev_pid": 1537,          # 1537=普通话(纯中文) 1737=英语 1637=粤语 ...
    }
    path = _config_path()
    if not os.path.isfile(path):
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (ValueError, OSError):
        return defaults
    saved = cfg.get("asr", {})
    if isinstance(saved, dict):
        defaults.update({k: v for k, v in saved.items() if k in defaults})
    return defaults


def set_asr_settings(settings: dict) -> dict:
    """合并保存语音转写设置；返回保存后的完整设置。"""
    current = get_asr_settings()
    current.update({k: v for k, v in (settings or {}).items() if k in current})
    path = _config_path()
    cfg = {}
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
    except (ValueError, OSError):
        cfg = {}
    cfg["asr"] = current
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return current

def get_db_dir() -> str | None:
    """Return the _db_dir (WeChat db_storage path) stored in config, or None."""
    path = _config_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        return cfg.get('_db_dir', '')
    except (ValueError, OSError):
        return None
