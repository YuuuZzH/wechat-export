"""Orchestrate the backup pipeline: scan -> decrypt -> migrate -> index."""
import os
import threading
from typing import Callable, Optional


ProgressCallback = Callable[[str, str, float], None]
# stage, detail, progress_0_to_1


def run_backup(
    db_storage_path: str,
    output_dir: str,
    key_file: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    link_dest: Optional[str] = None,
    on_progress: ProgressCallback = None,
    harvest_keys: bool = True,
    incremental: bool = True,
) -> dict:
    """Run the full backup pipeline.

    Args:
        db_storage_path: WeChat db_storage directory
        output_dir: Backup output root (e.g., D:\\WeChat_Backup\\2026-05-17)
        key_file: Path to legacy key JSON file (None = auto-detect from config)
        start_date: YYYY-MM-DD start of message date range (None = unbounded)
        end_date: YYYY-MM-DD end of message date range (None = unbounded)
        link_dest: Previous backup root. When set, unchanged DBs and media
            files are reused (hardlink) from that directory.
        on_progress: callback(stage, detail, progress)
        harvest_keys: If True and WeChat is running, harvest V2 AES keys
            in background during backup
        incremental: When True (default), auto-resolve link_dest from the
            last successful backup and skip re-decrypting unchanged DBs.

    Returns:
        {success: bool, stats: {..., v2_keys_harvested: int}, errors: [...]}
    """
    if on_progress is None:
        on_progress = lambda s, d, p: None

    result = {'success': False, 'stats': {}, 'errors': []}
    if not os.path.isdir(db_storage_path):
        result['errors'].append(f'db_storage_path 不存在或不可访问: {db_storage_path}')
        return result

    account = None
    resolved_link = None

    # --- Background: V2 key harvester (if WeChat is running) ---
    harvester_thread = None
    harvester_stop = None
    harvester_result = [{}]  # mutable container for thread result

    if harvest_keys:
        try:
            from engine.services.v2_key_extract import is_wechat_running as _wx_running
            if _wx_running():
                on_progress('scan', '微信正在运行，后台收割 V2 图片密钥中...', 0.0)
                harvester_stop = threading.Event()

                def _run_harvester():
                    from engine.services.v2_key_extract import harvest_v2_keys
                    try:
                        harvester_result[0] = harvest_v2_keys(
                            output_dir,
                            wxid=None,
                            interval=1.5,
                            max_rounds=None,
                            print_fn=lambda m: on_progress('harvest', m, 0.5),
                            stop_event=harvester_stop,
                        )
                    except Exception:
                        pass

                harvester_thread = threading.Thread(
                    target=_run_harvester, daemon=True)
                harvester_thread.start()
                on_progress('scan', '后台 V2 密钥收割已启动，请在微信中滚动浏览图片', 0.05)
        except ImportError:
            pass

    # Stage 1: Scan
    try:
        on_progress('scan', 'Detecting WeChat installation...', 0.1)
        from .scanner import scan_accounts
        account = scan_accounts(db_storage_path)
        on_progress('scan', f'Found account: {account.wxid}', 1.0)
    except Exception as e:
        result['errors'].append(f'Scan failed: {e}')
        _stop_harvester(harvester_stop, harvester_thread, harvester_result)
        return result

    # Resolve previous backup for incremental reuse
    try:
        from .fingerprint import resolve_link_dest
        resolved_link = resolve_link_dest(
            output_dir,
            incremental=incremental,
            link_dest=link_dest,
            wxid=getattr(account, 'wxid', None),
        )
        if resolved_link:
            result['stats']['link_dest'] = resolved_link
            mode = '增量' if incremental else '指定复用'
            on_progress('scan', f'{mode}基线: {resolved_link}', 1.0)
        elif incremental:
            on_progress('scan', '未找到上次备份，本次将执行全量解密', 1.0)
    except Exception:
        resolved_link = link_dest if link_dest and os.path.isdir(link_dest) else None

    # Stage 2: Decrypt
    try:
        on_progress('decrypt', 'Loading decryption keys...', 0.0)
        from .decryptor import load_keys, decrypt_for_backup
        keys = load_keys(key_file)
        if not keys:
            # Auto-extract keys from WeChat process memory
            on_progress('decrypt', '未找到密钥，正在从微信进程自动提取...', 0.05)
            try:
                import key_scan
                key_scan.run_key_scan(db_storage_path, None,
                    print_fn=lambda m: on_progress('decrypt', m, 0.1),
                    progress_fn=lambda pct, msg: on_progress('decrypt', msg, 0.1 + pct * 0.15 / 100))
                keys = load_keys(None)
            except Exception as ke:
                result['errors'].append(
                    f'未找到数据库密钥且自动提取失败: {ke}。'
                    f'请确保微信(Weixin.exe/WeChat.exe)正在运行，'
                    f'然后在 Web 页面执行"密钥提取"')
                _stop_harvester(harvester_stop, harvester_thread, harvester_result)
                return result
            if not keys:
                result['errors'].append(
                    '自动密钥提取未获得任何密钥。'
                    '请确保微信正在运行，然后在 Web 页面执行"密钥提取"')
                _stop_harvester(harvester_stop, harvester_thread, harvester_result)
                return result

        on_progress('decrypt', f'Decrypting with {len(keys)} keys...', 0.2)
        decrypted, skipped_keys, dec_stats = decrypt_for_backup(
            db_storage_path, output_dir, keys,
            on_progress=lambda d, p: on_progress('decrypt', d, p * 0.6 + 0.2),
            link_dest=resolved_link,
        )

        # If some DBs couldn't be decrypted, try extracting keys from memory
        if skipped_keys:
            on_progress('decrypt',
                f'{len(skipped_keys)} 数据库缺密钥，正在从微信进程自动提取...', 0.82)
            try:
                import key_scan
                key_scan.run_key_scan(db_storage_path, None,
                    print_fn=lambda m: on_progress('decrypt', m, 0.84),
                    progress_fn=lambda pct, msg: on_progress('decrypt', msg, 0.84 + pct * 0.04 / 100))
                keys = load_keys(None)
                on_progress('decrypt',
                    f'密钥已更新 ({len(keys)} 个), 重试解密...', 0.90)
                decrypted2, skipped_keys, dec_stats2 = decrypt_for_backup(
                    db_storage_path, output_dir, keys,
                    on_progress=lambda d, p: on_progress('decrypt', d, p * 0.09 + 0.90),
                    link_dest=resolved_link,
                )
                decrypted = list(set(decrypted + decrypted2))
                dec_stats = {
                    'reused': int(dec_stats.get('reused', 0)) + int(dec_stats2.get('reused', 0)),
                    'decrypted': int(dec_stats.get('decrypted', 0)) + int(dec_stats2.get('decrypted', 0)),
                    'fingerprint': max(int(dec_stats.get('fingerprint', 0)),
                                       int(dec_stats2.get('fingerprint', 0))),
                }
            except Exception:
                pass  # keep original results; already reported via skipped_keys

        result['stats']['decrypted'] = len(decrypted)
        result['stats']['db_reused'] = int(dec_stats.get('reused', 0))
        result['stats']['db_fresh'] = int(dec_stats.get('decrypted', 0))
        if skipped_keys:
            result['stats']['skipped_missing_key'] = skipped_keys
        on_progress(
            'decrypt',
            f"数据库就绪 {len(decrypted)}（新解密 {result['stats']['db_fresh']} / 复用 {result['stats']['db_reused']}）",
            1.0)
    except Exception as e:
        result['errors'].append(f'Decrypt failed: {e}')
        _stop_harvester(harvester_stop, harvester_thread, harvester_result)
        return result

    # Stage 3: Migrate media
    try:
        on_progress('migrate', 'Migrating media files...', 0.0)
        from .migrator import migrate_media
        media_stats = migrate_media(
            db_storage_path, output_dir,
            start_date=start_date, end_date=end_date,
            link_dest=resolved_link,
            on_progress=lambda f, n, t: on_progress('migrate', f, n / max(t, 1))
        )
        result['stats']['migrated'] = media_stats
        on_progress('migrate',
            f"Media: {media_stats.get('hardlinked', 0)} hardlinked, "
            f"{media_stats.get('link_reused', 0)} reused, "
            f"{media_stats.get('copied', 0)} copied",
            1.0)
    except Exception as e:
        result['errors'].append(f'Media migration failed: {e}')

    # Stage 4: Index
    try:
        on_progress('index', 'Building search index...', 0.0)
        from .indexer import build_index
        index_path = os.path.join(output_dir, 'data', 'chats.db')
        build_index(output_dir, index_path,
                    start_date=start_date, end_date=end_date,
                    on_progress=lambda d, p: on_progress('index', d, p))
        result['stats']['indexed'] = index_path
        on_progress('index', 'Index complete', 1.0)
    except Exception as e:
        result['errors'].append(f'Index build failed: {e}')

    # --- Stop harvester and collect results ---
    v2_count = _stop_harvester(harvester_stop, harvester_thread, harvester_result)
    if v2_count > 0:
        result['stats']['v2_keys_harvested'] = v2_count
        on_progress('done', f'Backup complete — {v2_count} V2 keys harvested', 1.0)

    if account:
        result['wxid'] = account.wxid
    result['success'] = len(result['errors']) == 0
    if not harvester_thread:
        on_progress('done', 'Backup complete', 1.0)
    return result


def _stop_harvester(stop_event, thread, result_container=None):
    """Signal harvester to stop, wait for it, and return key count."""
    if stop_event is None or thread is None:
        return 0
    stop_event.set()
    thread.join(timeout=5.0)
    if result_container and result_container[0]:
        return len(result_container[0])
    return 0

