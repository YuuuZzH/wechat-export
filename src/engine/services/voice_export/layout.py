# -*- coding: utf-8 -*-
"""写盘层：逐条音频、合并音频（含体积保护）、清单、缺失清单、README、zip。

约定：

* 时长与偏移**以 PCM 长度为准**；合并写盘时把真实时长/偏移**回填**到 ``VoiceItem``；
* 合并分组超过 ``max_bytes`` 时自动按顺序切成 ``_第N批``（避免为一个人生成几 GB 的单文件）；
* 单条取不到 PCM 时记 ``item.error``，由上层汇总进 ``missing.csv``（不静默）。
"""

import csv
import json
import os
import zipfile

from engine.services.media import silk_to_pcm as _silk_to_pcm
from . import audio
from .model import safe_filename, voice_basename

MANIFEST_FIELDS = ['sender_name', 'sender_id', 'chat_name', 'chat_id', 'datetime',
                   'duration_s', 'sample_rate', 'file', 'merged_file',
                   'offset_start', 'offset_end', 'transcript', 'status']

#: 单个合并文件的目标上限（PCM 字节）。超过就自动切批，避免内存/单文件过大。
DEFAULT_MAX_BYTES = 300 * 1024 * 1024

#: MP3 编码质量（LAME 0=最好最慢 … 9=最快）。
#: 真机测量：600 秒音频 quality=5 要 5.5s、quality=7 只要 1.5s，**比特率与体积完全相同**
#: （64kbps CBR）。语音场景 7 与 5 听感差异可忽略，所以默认取 7。
DEFAULT_MP3_QUALITY = 7


def _encode(pcm, path_no_ext, fmt, sample_rate, mp3_quality=DEFAULT_MP3_QUALITY):
    """按格式编码；mp3 不可用时**降级 WAV**（并在上层日志里说明）。"""
    if fmt == 'mp3':
        got = audio.encode_mp3(pcm, path_no_ext + '.mp3', sample_rate,
                               quality=mp3_quality)
        return got or audio.encode_wav(pcm, path_no_ext + '.wav', sample_rate)
    if fmt == 'm4a':
        return audio.encode_m4a(pcm, path_no_ext + '.m4a', sample_rate)
    return audio.encode_wav(pcm, path_no_ext + '.wav', sample_rate)


def _unique_name(used, directory, base, ext):
    key = (directory, base)
    used[key] = used.get(key, 0) + 1
    if used[key] > 1:
        base = '%s_%d' % (base, used[key])
    return base


def _pcm_of(item, pcm_cache=None):
    """取一条语音的 PCM：优先用采集阶段缓存好的（**只解码一次**），没有再解码并顺手缓存。"""
    if pcm_cache is not None and item.pcm_path:
        cached = pcm_cache.get(item.pcm_path)
        if cached:
            return cached
    pcm = _silk_to_pcm(item.silk_path)
    if pcm and pcm_cache is not None and not item.pcm_path:
        item.pcm_path = pcm_cache.put(pcm_cache.key_for(item.create_time, item.local_id),
                                      pcm) or ''
    return pcm


def write_individual(items, out_dir, fmt='mp3', sample_rate='auto', keep_silk=False,
                     progress_fn=None, pcm_cache=None, workers=1,
                     mp3_quality=DEFAULT_MP3_QUALITY):
    """逐条写出 ``audio/<姓名>/<姓名>_YYYYMMDD-HHMMSS.<ext>``。

    命名与去重先**串行**算好（保证可复现），再并行编码写出（编码是 CPU/子进程，可并行）。
    """
    jobs, used = [], {}
    total = len([i for i in items if i.silk_path and not i.error])
    for item in items:
        if not item.silk_path or item.error:
            continue
        rate = item.sample_rate if sample_rate == 'auto' else int(sample_rate)
        name_dir = safe_filename(item.sender_name or 'unknown')
        # 命名规则：<姓名>_YYYYMMDD-HHMMSS（plan_merge 已算过就直接用）
        base = item.out_name or voice_basename(item.sender_name or name_dir, item.create_time)
        base = _unique_name(used, name_dir, base, '')
        item.out_name = base
        jobs.append((item, rate, name_dir, base))

    def _one(job):
        item, rate, name_dir, base = job
        pcm = _pcm_of(item, pcm_cache)
        if not pcm:
            item.error = 'SILK 解码失败'
            return None
        target_dir = os.path.join(out_dir, 'audio', name_dir)
        os.makedirs(target_dir, exist_ok=True)
        path = _encode(pcm, os.path.join(target_dir, base), fmt, rate, mp3_quality)
        if keep_silk and os.path.isfile(item.silk_path):
            keep_dir = os.path.join(out_dir, 'original-silk', name_dir)
            os.makedirs(keep_dir, exist_ok=True)
            dst = os.path.join(keep_dir, base + '.silk')
            if not os.path.isfile(dst):
                with open(item.silk_path, 'rb') as src, open(dst, 'wb') as out:
                    out.write(src.read())
        return {'item': item,
                'file': os.path.relpath(path, out_dir).replace(os.sep, '/'),
                'merged_file': '', 'status': 'ok'}

    if workers and workers > 1 and len(jobs) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=int(workers)) as pool:
            done = list(pool.map(_one, jobs))
    else:
        done = [_one(job) for job in jobs]
    layouts = [row for row in done if row]
    if progress_fn:
        progress_fn('write', '已写出 %d/%d 条' % (len(layouts), total))
    return layouts


def _plan_chunks(group, max_bytes, gap_s):
    """把一个分组按预估体积切成若干批（每批不超过 max_bytes）。"""
    estimate = 2 * group.sample_rate          # 每秒字节数（16-bit 单声道）
    per_gap = audio.silence_pcm(gap_s, group.sample_rate)
    chunks, current, size = [], [], 0
    for item in group.items:
        item_bytes = int(item.duration_s * estimate) + len(per_gap)
        if current and size + item_bytes > max_bytes:
            chunks.append(current)
            current, size = [], 0
        current.append(item)
        size += item_bytes
    if current:
        chunks.append(current)
    return chunks


def write_merged(groups, out_dir, fmt='mp3', gap_s=1.0, max_bytes=DEFAULT_MAX_BYTES,
                 progress_fn=None, pcm_cache=None, mp3_quality=DEFAULT_MP3_QUALITY,
                 workers=1):
    """按分组写合并音频；超大分组自动切批；**多组时并行**（每组一条独立编码流）。

    Returns: ``[{'group','file','duration_s','items','split'}]``（``items`` 为该文件包含的语音）
    """
    merged_dir = os.path.join(out_dir, 'merged')
    os.makedirs(merged_dir, exist_ok=True)

    def _one_group(group):
        """把一组语音合成一个（或按上限切成几个）文件；组内必须顺序编码。"""
        out = []
        chunks = _plan_chunks(group, max_bytes, gap_s) if max_bytes else [group.items]
        for idx, chunk in enumerate(chunks):
            segments, dst_rate, decoded = [], group.sample_rate, []
            for item in chunk:
                pcm = _pcm_of(item, pcm_cache)
                if not pcm:
                    item.error = 'SILK 解码失败'
                    continue
                segments.append((pcm, item.sample_rate))
                decoded.append((item, pcm))
            if not segments:
                continue
            merged = audio.concat_pcm(segments, gap_s, dst_rate)
            # 按**真实 PCM 时长**回填偏移（plan_merge 的值只是预估）
            position = 0.0
            for i, (item, pcm) in enumerate(decoded):
                if i:
                    position += gap_s
                item.offset_start = position
                item.duration_s = audio.pcm_duration_s(pcm, item.sample_rate)
                item.offset_end = position + item.duration_s
                position = item.offset_end
            stem = group.file_stem if len(chunks) == 1 else '%s_第%d批' % (group.file_stem, idx + 1)
            path = _encode(merged, os.path.join(merged_dir, stem), fmt, dst_rate, mp3_quality)
            out.append({'group': group,
                        'file': os.path.relpath(path, out_dir).replace(os.sep, '/'),
                        'duration_s': audio.pcm_duration_s(merged, dst_rate),
                        'items': [item for item, _pcm in decoded],
                        'split': len(chunks) > 1})
            if progress_fn:
                progress_fn('merge', '已合并 %s（%d 条）' % (stem, len(decoded)))
            del merged                # 及时释放大缓冲
        return out

    if workers and workers > 1 and len(groups) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(int(workers), len(groups))) as pool:
            nested = list(pool.map(_one_group, groups))
        return [row for rows in nested for row in rows]
    results = []
    for group in groups:
        results.extend(_one_group(group))
    return results


def write_manifest(items, out_dir, layouts=None, merged=None):
    """写出 ``manifest.csv`` / ``manifest.json``（含每条的合并文件与起止偏移）。"""
    file_map = {}
    for lay in (layouts or []):
        it = lay.get('item')
        if it is not None:
            file_map[id(it)] = lay.get('file', '')
    merged_map = {}
    for res in (merged or []):
        for it in res.get('items', []):
            merged_map[id(it)] = res.get('file', '')
    rows = []
    for it in items:
        rows.append({
            'sender_name': it.sender_name, 'sender_id': it.sender_id,
            'chat_name': it.chat_name, 'chat_id': it.chat_id,
            'datetime': it.datetime_text, 'duration_s': '%.2f' % it.duration_s,
            'sample_rate': it.sample_rate, 'file': file_map.get(id(it), ''),
            'merged_file': merged_map.get(id(it), ''),
            'offset_start': '%.2f' % it.offset_start, 'offset_end': '%.2f' % it.offset_end,
            'transcript': it.transcript, 'status': 'error' if it.error else 'ok',
        })
    csv_path = os.path.join(out_dir, 'manifest.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    json_path = os.path.join(out_dir, 'manifest.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({'count': len(rows), 'items': rows}, f, ensure_ascii=False, indent=2)
    return csv_path, json_path


def write_missing(missing, out_dir):
    """写出 ``missing.csv``（取不到的语音 + 原因）。"""
    path = os.path.join(out_dir, 'missing.csv')
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['sender_name', 'chat_name', 'datetime', 'reason'])
        writer.writeheader()
        for row in missing:
            writer.writerow({k: row.get(k, '') for k in writer.fieldnames})
    return path


def write_readme(out_dir, title='语音导出'):
    """写一句话说明，保证"解压后知道怎么打开"。"""
    path = os.path.join(out_dir, 'README.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('%s\n\n' % title)
        f.write('1) 双击 index.html 即可在浏览器里按人查看并播放（不需要本程序、不需要联网）。\n')
        f.write('2) audio/ 是逐条语音文件；merged/ 是按规则合并后的文件。\n')
        f.write('3) manifest.csv / manifest.json 是清单（含每条在合并文件中的起止秒数）。\n')
        f.write('4) missing.csv 列出在备份里找不到的语音（可重跑一次「一键备份」后再导出）。\n')
        f.write('5) all-in-one.html 是"音频内嵌"的单文件版，适合直接发给家人。\n')
    return path


#: 已经压缩过/无需再压的扩展名 —— 对它们用 ZIP_STORED。
#: 真机测量：对 mp3 再压缩会让打包从 <1s 变成 9s，体积几乎不变。
_STORE_EXT = {'.mp3', '.m4a', '.aac', '.wav', '.silk', '.amr',
              '.png', '.jpg', '.jpeg', '.gif', '.webp', '.zip', '.gz', '.zst'}


def make_zip(out_dir, zip_path):
    """把导出目录打包成 zip（供浏览器一键下载）。

    音频/图片按 **store**（不压缩）写入，文本类（html/csv/json/txt）才压缩。
    """
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(out_dir):
            for name in files:
                full = os.path.join(root, name)
                arcname = os.path.relpath(full, out_dir)
                ext = os.path.splitext(name)[1].lower()
                method = zipfile.ZIP_STORED if ext in _STORE_EXT else zipfile.ZIP_DEFLATED
                zf.write(full, arcname, compress_type=method)
    return zip_path
