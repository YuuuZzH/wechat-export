"""Per-type message XML parsers."""
import re
import html
import xml.etree.ElementTree as ET
from engine.parsers import register, PARSERS


def _safe_xml(xml_bytes):
    """Parse XML bytes safely, returning root element or None."""
    if not xml_bytes:
        return None
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None


def _wx4_re_extract(xml_bytes, patterns, raw_bytes=None):
    """Extract fields from WeChat 4.x protobuf-corrupted XML using regex.

    WeChat 4.x embeds protobuf field tags inside XML, breaking standard parsing.
    Uses regex on the corrupted string for simple tag extraction, and raw byte
    extraction for CDATA sections (which contain valid UTF-8 Chinese text
    surrounded by protobuf binary garbage).

    Args:
        xml_bytes: bytes to extract from
        patterns: dict of name -> regex pattern (applied to decoded string)
        raw_bytes: original bytes for CDATA extraction (if different from xml_bytes)
    Returns a dict of extracted fields, or empty dict if nothing found.
    """
    raw = raw_bytes if raw_bytes else xml_bytes
    if not raw:
        return {}

    try:
        s = raw.decode('utf-8', errors='replace')
    except (UnicodeError, AttributeError):
        return {}

    results = {}
    for name, pattern in patterns.items():
        m = re.search(pattern, s)
        if m:
            val = m.group(1)
            if val:
                results[name] = val

    # Extract CDATA content from raw bytes by finding markers in bytes
    if isinstance(raw, bytes):
        cdata_start = raw.find(b'<![CDATA[')
        if cdata_start >= 0:
            cdata_begin = cdata_start + len(b'<![CDATA[')
            cdata_end = raw.find(b']]>', cdata_begin)
            if cdata_end > cdata_begin:
                cdata_bytes = raw[cdata_begin:cdata_end]
                try:
                    cdata_text = cdata_bytes.decode('utf-8', errors='replace')
                    if cdata_text.strip():
                        results['_cdata'] = cdata_text.strip()
                except UnicodeError:
                    pass

    return results


@register(1)
def parse_text(xml_bytes):
    return {'text': None}  # text messages use Content field directly


@register(3)
def parse_image(xml_bytes):
    root = _safe_xml(xml_bytes)
    if root is None:
        return {'text': '[图片]', 'image_path': None, 'thumb_path': None}
    # Root may be <img> directly (WeChat 4.x zstd XML) or <msg><img>
    img = root if root.tag == 'img' else root.find('.//img')
    if img is None:
        return {'text': '[图片]', 'image_path': None, 'thumb_path': None}
    big = img.get('big') or img.get('cdnbigimgurl') or img.get('hdlength')
    thumb = img.get('thumb') or img.get('cdnthumburl')
    aeskey = img.get('aeskey') or img.get('aes_key') or img.get('cdnthumbaeskey')
    return {
        'text': '[图片]',
        'image_path': big,
        'thumb_path': thumb,
        'aeskey': aeskey,
        'cdnthumbaeskey': img.get('cdnthumbaeskey'),
        'cdnthumburl': img.get('cdnthumburl'),
        'encryver': img.get('encryver'),
        'width': int(img.get('width', 0) or img.get('cdnthumbwidth', 0)) if (img.get('width') or img.get('cdnthumbwidth')) else None,
        'height': int(img.get('height', 0) or img.get('cdnthumbheight', 0)) if (img.get('height') or img.get('cdnthumbheight')) else None,
    }


@register(6)
def parse_file(xml_bytes):
    root = _safe_xml(xml_bytes)
    if root is None:
        return {'text': '[文件]', 'file_path': None, 'title': None, 'size': None}
    # Root might be <appmsg> directly or <msg><appmsg>
    appmsg = root if root.tag == 'appmsg' else root.find('.//appmsg')
    title = appmsg.findtext('title') if appmsg is not None else None
    attach = root.find('.//appattach')
    return {
        'text': f'[文件] {title or ""}',
        'title': title,
        'file_path': attach.get('cdnattachurl') if attach is not None else None,
        'size': int(attach.get('totallen', 0)) if attach is not None and attach.get('totallen') else None,
        'ext': attach.get('fileext') if attach is not None else None,
    }


@register(34)
def parse_voice(xml_bytes):
    root = _safe_xml(xml_bytes)
    if root is None:
        return {'text': '[语音]', 'voice_path': None, 'duration': None}
    voicemsg = root.find('.//voicemsg')
    if voicemsg is None:
        return {'text': '[语音]', 'voice_path': None, 'duration': None}
    # voicelength 是毫秒（WeChat 4.x），统一换算成秒再交给上层；length 是音频
    # 字节数，不能当秒用（历史上这里两者混用，导致界面显示错误时长）。
    raw_len = (voicemsg.get('voicelength') or '').strip()
    ms = int(raw_len) if raw_len.isdigit() else 0
    return {
        'text': '[语音]',
        'voice_path': voicemsg.get('voiceurl') or voicemsg.get('bufid'),
        'duration': int(round(ms / 1000.0)) if ms > 0 else None,
        'duration_ms': ms or None,
    }


@register(43)
def parse_video(xml_bytes):
    root = _safe_xml(xml_bytes)
    if root is None:
        return {'text': '[视频]', 'thumb_path': None, 'duration': None}
    video = root.find('.//videomsg') or root.find('.//video')
    if video is None:
        video = root if root.tag in ('videomsg', 'video') else None
    if video is not None:
        # playlength = duration in seconds; length = file size in bytes (NOT duration)
        dur = video.get('playlength') or video.get('videotime')
        return {
            'text': '[视频]',
            'thumb_path': video.get('cdnthumburl'),
            'duration': int(dur) if dur else None,
            'aeskey': video.get('aeskey') or video.get('aes_key'),
            'cdnvideourl': video.get('cdnvideourl'),
            'cdnthumbaeskey': video.get('cdnthumbaeskey'),
            'size': int(video.get('length', 0)) if video.get('length') else None,
            'thumb_width': int(video.get('cdnthumbwidth', 0)) if video.get('cdnthumbwidth') else None,
            'thumb_height': int(video.get('cdnthumbheight', 0)) if video.get('cdnthumbheight') else None,
        }
    return {'text': '[视频]', 'thumb_path': None, 'duration': None}


@register(48)
def parse_location(xml_bytes):
    root = _safe_xml(xml_bytes)
    if root is not None:
        loc = root.find('.//location')
        if loc is not None:
            return {
                'text': f'[位置] {loc.get("poiname", loc.get("label", ""))}',
                'lat': float(loc.get('x', 0)) if loc.get('x') else None,
                'lng': float(loc.get('y', 0)) if loc.get('y') else None,
                'label': loc.get('label'),
                'poiname': loc.get('poiname'),
            }

    # WeChat 4.x: XML corrupted by protobuf tags — use regex extraction
    xx = _wx4_re_extract(xml_bytes, {
        'poiname': r'poiname="(.*?)"',
        'label': r'label="(.*?)"',
        'x': r'x="([\d.]+)"',
        'y': r'y="([\d.]+)"',
        'poiname_tag': r'<poiname>(.*?)</',
        'label_tag': r'<label>(.*?)</',
    })
    if not xx:
        return {'text': '[位置]', 'lat': None, 'lng': None, 'label': None, 'poiname': None}

    poiname = xx.get('poiname') or xx.get('poiname_tag') or xx.get('_cdata', '')
    label = xx.get('label') or xx.get('label_tag') or ''
    lat = float(xx['x']) if xx.get('x') else None
    lng = float(xx['y']) if xx.get('y') else None

    display = f'[位置] {poiname or label or ""}'
    return {
        'text': display,
        'lat': lat,
        'lng': lng,
        'label': label or None,
        'poiname': poiname or None,
    }


def _find_tag_positions(text: str, tag: str, start: int = 0) -> tuple:
    """Find next occurrence of opening tag and closing tag from start.

    Uses regex word-boundary check to avoid matching prefixes (e.g.
    ``<dataitem`` must not match ``<dataitemsource``).  Returns
    (open_pos, close_pos) where -1 means not found.
    """
    # Match <tag followed by space, >, or newline — but not other alphanum chars
    open_pat = re.compile(rf'<{tag}(?:\s|>|\n)')
    close_pat = re.compile(rf'</{tag}>')

    om = open_pat.search(text, start)
    cm = close_pat.search(text, start)
    open_pos = om.start() if om else -1
    close_pos = cm.start() if cm else -1
    return open_pos, close_pos


def _find_matching_close(text: str, tag: str, start: int) -> int:
    """Find matching closing tag position for an opening tag at ``start``.

    Tracks nesting depth to handle inner occurrences of the same tag.
    Returns the position of the matching ``</tag>`` or -1.
    """
    depth = 1
    pos = start + 1
    while pos < len(text):
        next_open, next_close = _find_tag_positions(text, tag, pos)
        if next_close < 0:
            return -1
        if next_open >= 0 and next_open < next_close:
            depth += 1
            pos = next_open + 1
        else:
            depth -= 1
            if depth == 0:
                return next_close
            pos = next_close + len(f'</{tag}>')
    return -1


def _split_dataitems(text: str, max_items: int = 99) -> list:
    """Split text into (attrs_str, body) pairs for each top-level <dataitem>.

    Handles nested <dataitem> tags by tracking depth, so dataitems that
    contain inner <dataitem> children (e.g. within <recordxml>) are
    correctly bounded.
    """
    results = []
    pos = 0
    while len(results) < max_items:
        open_pos, _ = _find_tag_positions(text, 'dataitem', pos)
        if open_pos < 0:
            break
        attrs_end = text.find('>', open_pos)
        if attrs_end < 0:
            break
        attrs = text[open_pos + len('<dataitem'):attrs_end]
        body_start = attrs_end + 1
        body_end = _find_matching_close(text, 'dataitem', open_pos)
        if body_end < 0:
            break
        body = text[body_start:body_end]
        results.append((attrs, body))
        pos = body_end + len('</dataitem>')
    return results


def _parse_recorditem_dataitems(recorditem_text: str, max_items: int = 99,
                                 depth: int = 0) -> list:
    """Parse HTML-escaped recorditem inner XML to extract dataitem records.

    Supports multi-level nesting: when a dataitem contains a <recordxml> or
    <recorditem> child (e.g. location shares, nested forwards), recursively
    parses the inner dataitems as ``nested_records`` on the parent record.

    WeChat 4.x encodes the <recorditem> inner XML with HTML entities
    (&lt; &gt; &amp;).  We unescape once then extract <dataitem> pairs.
    """
    if not recorditem_text or depth > 4:  # depth guard against infinite recursion
        return []

    # HTML-unescape: &lt;→<, &gt;→>, &amp;→&
    unescaped = html.unescape(recorditem_text)

    dataitems = _split_dataitems(unescaped, max_items)
    if not dataitems:
        return []

    records = []
    for attrs_str, di_body in dataitems:
        rec = {}

        dtm = re.search(r'datatype="(\d+)"', attrs_str)
        rec['datatype'] = int(dtm.group(1)) if dtm else 0

        didm = re.search(r'dataid="([^"]*)"', attrs_str)
        if didm:
            rec['dataid'] = didm.group(1)

        # Text fields
        for field in ['sourcename', 'datadesc', 'datatitle', 'sourcetime',
                      'sourceheadurl', 'cdnthumburl', 'fullmd5', 'cdndatakey',
                      'thumbsize', 'datasize', 'cdnthumbkey', 'datafmt',
                      'cdnencryver', 'cdnthumbheight', 'cdnthumbwidth',
                      'duration', 'srcChatname']:
            fm = re.search(rf'<{field}>(.*?)</{field}>', di_body)
            if fm:
                val = html.unescape(fm.group(1).strip())
                if val:
                    rec[field] = val

        # Numeric fields
        for nf in ('thumbsize', 'datasize', 'duration'):
            if nf in rec:
                try:
                    rec[nf] = int(rec[nf])
                except (ValueError, TypeError):
                    del rec[nf]

        # Recursive nested: <recordxml> (location share) or
        # HTML-escaped <recorditem> inside the body (nested forward)
        nested_source = None
        rxml_match = re.search(r'<recordxml>', di_body)
        if rxml_match:
            body_start = rxml_match.end()
            body_end = _find_matching_close(di_body, 'recordxml', rxml_match.start())
            if body_end > 0:
                nested_source = di_body[body_start:body_end]
        if not nested_source:
            # Check for &lt;recorditem&gt; in the body (double-escaped)
            ri_escaped = re.search(r'&lt;recorditem&gt;(.*?)&lt;/recorditem&gt;',
                                   di_body, re.DOTALL)
            if ri_escaped:
                nested_source = ri_escaped.group(1)

        if nested_source:
            nested = _parse_recorditem_dataitems(nested_source, max_items, depth + 1)
            if nested:
                rec['nested_records'] = nested

        records.append(rec)

    return records


@register(49)
def parse_link(xml_bytes):
    """Parse type 49 app messages: links, files, transfers, red packets, mini programs, etc."""
    root = _safe_xml(xml_bytes)

    if root is not None:
        appmsg = root if root.tag == 'appmsg' else root.find('.//appmsg')
        if appmsg is not None:
            msg_type = int(appmsg.findtext('type', '0') or '0')
            title = appmsg.findtext('title', '') or ''
            des = appmsg.findtext('des', '') or ''
            url = appmsg.findtext('url', '') or ''
            appname = appmsg.findtext('appname', '') or ''
            sourcedisplayname = appmsg.findtext('sourcedisplayname', '') or appname

            # --- Type 8: forwarded messages ---
            if msg_type == 8:
                ri_text = appmsg.findtext('recorditem', '') or ''
                records = _parse_recorditem_dataitems(ri_text)
                return {
                    'text': f'[转发] {title}' if title else '[转发]',
                    'title': title or None,
                    'des': des or None,
                    'url': url or None,
                    'appname': appname or None,
                    'is_forward': True,
                    'forward_msg_count': len(records) if records else None,
                    'records': records or None,
                    'app_type': msg_type,
                    'render_type': 'forward',
                }

            # --- Type 19: merged chat history ---
            if msg_type == 19:
                ri_text = appmsg.findtext('recorditem', '') or ''
                records = _parse_recorditem_dataitems(ri_text)
                return {
                    'text': f'[聊天记录] {title}' if title else '[聊天记录]',
                    'title': title or '聊天记录',
                    'des': des or None,
                    'app_type': 19,
                    'render_type': 'chat_history',
                    'records': records or None,
                }

            # --- Type 4, 5, 68: link shares (articles, videos, etc.) ---
            if msg_type in (4, 5, 68) and url:
                thumburl = appmsg.findtext('thumburl', '') or appmsg.findtext('cdnthumburl', '')
                return {
                    'text': f'[链接] {title}' if title else '[链接]',
                    'title': title or None,
                    'des': des or None,
                    'url': url or None,
                    'appname': appname or None,
                    'sourcedisplayname': sourcedisplayname or None,
                    'thumburl': thumburl or None,
                    'app_type': msg_type,
                    'render_type': 'link',
                }

            # --- Type 51: finder/channels video ---
            if msg_type == 51:
                # WeChat 4.x may show "当前版本不支持展示该内容" when it can't
                # render newer finder formats. Try title→des→sourcedisplayname→appname.
                candidates = [title, des, sourcedisplayname, appname]
                display_title = ''
                for c in candidates:
                    c = (c or '').strip()
                    if c and '不支持' not in c and '升级' not in c and '版本' not in c:
                        display_title = c
                        break
                if not display_title:
                    display_title = '视频号内容'
                return {
                    'text': f'[视频号] {display_title}',
                    'title': display_title,
                    'des': des or None,
                    'url': url or None,
                    'appname': appname or sourcedisplayname or None,
                    'app_type': 51,
                    'render_type': 'finder',
                }

            # --- Type 33, 36: mini program ---
            if msg_type in (33, 36):
                thumburl = appmsg.findtext('thumburl', '') or appmsg.findtext('cdnthumburl', '')
                return {
                    'text': f'[小程序] {title}' if title else '[小程序]',
                    'title': title or None,
                    'des': des or None,
                    'url': url or None,
                    'appname': appname or sourcedisplayname or None,
                    'thumburl': thumburl or None,
                    'app_type': msg_type,
                    'render_type': 'mini_program',
                }

            # --- Type 6: file share via appmsg ---
            if msg_type == 6:
                totallen = appmsg.findtext('totallen', '')
                attach = root.find('.//appattach')
                return {
                    'text': f'[文件] {title}' if title else '[文件]',
                    'title': title or None,
                    'size': int(totallen) if totallen and totallen.isdigit() else None,
                    'file_path': attach.get('cdnattachurl') if attach is not None and attach.get('cdnattachurl') else None,
                    'file_ext': attach.get('fileext') if attach is not None else None,
                    'aeskey': attach.get('aeskey') if attach is not None else None,
                    'app_type': 6,
                    'render_type': 'file',
                }

            # --- Type 57 or has refermsg: quote/reply ---
            refermsg = appmsg.find('refermsg')
            has_refermsg = refermsg is not None or '<refermsg' in (xml_bytes if isinstance(xml_bytes, str) else xml_bytes.decode('utf-8', errors='replace'))
            if msg_type == 57 or has_refermsg:
                reply_text = title
                ref_content = ''
                ref_username = ''
                if refermsg is not None:
                    ref_content = refermsg.findtext('content', '') or ''
                    ref_username = refermsg.findtext('fromusr', '') or refermsg.findtext('fromusername', '') or refermsg.get('fromusr', '')
                return {
                    'text': f'[引用] {reply_text}' if reply_text else '[引用消息]',
                    'title': reply_text or None,
                    'quote_content': ref_content or None,
                    'quote_username': ref_username or None,
                    'app_type': 57,
                    'render_type': 'quote',
                }

            # --- Type 62: nudge (拍一拍) ---
            if msg_type == 62:
                return {
                    'text': '[拍一拍]',
                    'app_type': 62,
                    'render_type': 'pat',
                }

            # --- Type 17: real-time location share ---
            if msg_type == 17:
                return {
                    'text': f'[位置共享] {title}' if title else '[位置共享]',
                    'title': title or None,
                    'app_type': 17,
                    'render_type': 'location_share',
                }

            # --- Type 2000: transfer (转账) ---
            if msg_type == 2000:
                feedesc = appmsg.findtext('feedesc', '') or ''
                pay_memo = appmsg.findtext('pay_memo', '') or ''
                return {
                    'text': f'[转账] {pay_memo}' if pay_memo else '[转账]',
                    'title': title or feedesc or None,
                    'des': pay_memo or des or None,
                    'amount': feedesc or '',
                    'app_type': 2000,
                    'render_type': 'transfer',
                }

            # --- Type 2001, 2003: red packet (红包) ---
            if msg_type in (2001, 2003):
                sendertitle = appmsg.findtext('sendertitle', '') or ''
                return {
                    'text': f'[红包] {sendertitle}' if sendertitle else '[红包]',
                    'title': sendertitle or title or None,
                    'app_type': msg_type,
                    'render_type': 'red_packet',
                }

            # --- Fallback: generic link/app message ---
            type_label = '转发' if msg_type == 8 else '链接'
            return {
                'text': f'[{type_label}] {title}' if title else f'[{type_label}]',
                'title': title or None,
                'des': des or None,
                'url': url or None,
                'appname': appname or None,
                'app_type': msg_type,
            }

    # WeChat 4.x: XML corrupted by protobuf tags — use regex extraction
    xx = _wx4_re_extract(xml_bytes, {
        'title': r'<title>(.*?)</',
        'des': r'<des>(.*?)</',
        'url': r'<url>(.*?)</',
        'type': r'<type>(\d+)</',
        'appname': r'<appname>(.*?)</',
        'sourcedisplayname': r'<sourcedisplayname>(.*?)</',
    })
    if not xx:
        return {'text': '[链接]', 'title': None, 'des': None, 'url': None, 'is_forward': False}

    cdata_title = xx.get('_cdata', '')
    title = cdata_title or xx.get('title', '')
    msg_type = int(xx.get('type', '0') or '0')
    has_refermsg = b'<refermsg' in xml_bytes if isinstance(xml_bytes, bytes) else '<refermsg' in (xml_bytes or '')

    # Parse recorditem for merged chat history (19) and forwards (8)
    records = None
    if msg_type in (8, 19):
        ri_raw = b'<recorditem' in xml_bytes if isinstance(xml_bytes, bytes) else '<recorditem' in (xml_bytes or '')
        if ri_raw:
            # Extract recorditem text (may be HTML-escaped)
            rim = re.search(rb'<recorditem>(.*?)</recorditem>', xml_bytes, re.DOTALL) if isinstance(xml_bytes, bytes) else \
                 re.search(r'<recorditem>(.*?)</recorditem>', xml_bytes, re.DOTALL)
            if rim:
                ri_text = rim.group(1)
                if isinstance(ri_text, bytes):
                    ri_text = ri_text.decode('utf-8', errors='replace')
                records = _parse_recorditem_dataitems(ri_text)

    if msg_type == 19:
        return {
            'text': f'[聊天记录] {title}' if title else '[聊天记录]',
            'title': title or '聊天记录',
            'des': xx.get('des'),
            'app_type': 19,
            'render_type': 'chat_history',
            'records': records or None,
        }

    if msg_type == 8:
        return {
            'text': f'[转发] {title}' if title else '[转发]',
            'title': title or None,
            'des': xx.get('des'),
            'url': xx.get('url'),
            'appname': xx.get('appname') or xx.get('sourcedisplayname'),
            'is_forward': True,
            'forward_msg_count': len(records) if records else None,
            'records': records or None,
            'app_type': 8,
            'render_type': 'forward',
        }

    is_forward = msg_type == 8
    has_recorditem = b'<recorditem' in xml_bytes if isinstance(xml_bytes, bytes) else '<recorditem' in (xml_bytes or '')

    # Try to extract file attachment info for subtype 6
    file_path = None
    file_ext = None
    file_aeskey = None
    if msg_type == 6:
        fp_m = re.search(r'cdnattachurl="([^"]*)"', s)
        if fp_m:
            file_path = fp_m.group(1)
        fe_m = re.search(r'fileext="([^"]*)"', s)
        if fe_m:
            file_ext = fe_m.group(1)
        ak_m = re.search(r'aeskey="([^"]*)"', s)
        if ak_m:
            file_aeskey = ak_m.group(1)

    if msg_type == 51:
        # Finder/channels video — WeChat 4.x may not render newer formats and
        # stores "当前版本不支持展示该内容" in the title. Try to find a better label.
        candidates = [title, xx.get('des', ''), xx.get('sourcedisplayname', ''), xx.get('appname', '')]
        display_title = ''
        for c in candidates:
            c = (c or '').strip()
            if c and '不支持' not in c and '升级' not in c and '版本' not in c:
                display_title = c
                break
        if not display_title:
            display_title = '视频号内容'
        type_label = '视频号'
    elif msg_type == 33 or msg_type == 36:
        type_label = '小程序'
    elif msg_type == 6:
        type_label = '文件'
    elif msg_type == 57 or has_refermsg:
        type_label = '引用'
    elif is_forward or has_recorditem:
        type_label = '转发'
    else:
        type_label = '链接'

    if msg_type == 51:
        display = f'[视频号] {display_title}'
    else:
        display = f'[{type_label}] {title}' if title else f'[{type_label}]'

    return {
        'text': display,
        'title': (display_title if msg_type == 51 else title) or None,
        'des': xx.get('des'),
        'url': xx.get('url'),
        'appname': xx.get('appname') or xx.get('sourcedisplayname'),
        'is_forward': is_forward or has_recorditem,
        'forward_msg_count': None,
        'app_type': msg_type,
        'file_path': file_path,
        'file_ext': file_ext,
        'aeskey': file_aeskey,
        'render_type': 'finder' if msg_type == 51 else ('file' if msg_type == 6 else None),
    }


@register(42)
def parse_contact_card(xml_bytes):
    root = _safe_xml(xml_bytes)
    if root is None:
        return {'text': '[名片]', 'username': None, 'nickname': None, 'alias': None}
    card = root if root.tag == 'msg' else root.find('.//msg')
    if card is None:
        return {'text': '[名片]', 'username': None, 'nickname': None, 'alias': None}
    return {
        'text': f'[名片] {card.get("nickname", "")}',
        'username': card.get('username'),
        'nickname': card.get('nickname'),
        'alias': card.get('alias'),
        'bigheadimgurl': card.get('bigheadimgurl') or card.get('big_img_url'),
        'smallheadimgurl': card.get('smallheadimgurl') or card.get('small_img_url'),
        'province': card.get('province'),
        'city': card.get('city'),
        'sign': card.get('sign'),
        'sex': int(card.get('sex', 0)) if card.get('sex') else None,
    }


@register(47)
def parse_emoji(xml_bytes):
    root = _safe_xml(xml_bytes)
    if root is None:
        return {'text': '[表情]', 'emoji_url': None}
    emoji = root.find('.//emoji')
    if emoji is None:
        # Root itself might be <emoji> or <msg><emoji>
        emoji = root if root.tag == 'emoji' else None
    if emoji is None:
        return {'text': '[表情]', 'emoji_url': None}
    md5 = emoji.get('md5')
    cdnurl = emoji.get('cdnurl')
    thumburl = emoji.get('thumburl')
    return {
        'text': '[表情]',
        'emoji_url': cdnurl,
        'thumb_url': thumburl,
        'md5': md5,
        'aeskey': emoji.get('aeskey') or emoji.get('aes_key'),
        'fromusername': emoji.get('fromusername'),
        'len': int(emoji.get('len', 0)) if emoji.get('len') else None,
        'productid': emoji.get('productid'),
        'type': emoji.get('type'),
    }


@register(50)
def parse_network_call(xml_bytes):
    """VoIP / network call message (WeChat 4.x zstd-decompressed XML)."""
    root = _safe_xml(xml_bytes)
    if root is None:
        return {'text': '[网络电话]'}

    # Pattern 1: VoipBubbleMsg (most common in 4.x)
    bubble = root.find('.//VoIPBubbleMsg')
    if bubble is not None:
        msg_text = bubble.findtext('msg', '')
        duration = bubble.findtext('duration', '0')
        msg_type = bubble.findtext('msg_type', '')
        dur_sec = int(duration) if duration and duration.isdigit() else 0
        if msg_text:
            return {
                'text': f'[网络电话] {msg_text}',
                'call_msg': msg_text,
                'duration': dur_sec,
                'msg_type': msg_type,
            }
        elif dur_sec > 0:
            dur_str = f'{dur_sec // 60}:{dur_sec % 60:02d}'
            return {'text': f'[网络电话] 通话时长 {dur_str}', 'duration': dur_sec}

    # Pattern 2: voiplocalinfo (older format)
    local = root.find('.//voiplocalinfo')
    if local is not None:
        display = local.findtext('diaplay_content') or local.findtext('display_content') or ''
        dur_text = local.findtext('duration', '0')
        dur_sec = int(dur_text) if dur_text and dur_text.isdigit() else 0
        if display:
            return {'text': f'[网络电话] {display}', 'duration': dur_sec, 'call_msg': display}
        elif dur_sec > 0:
            dur_str = f'{dur_sec // 60}:{dur_sec % 60:02d}'
            return {'text': f'[网络电话] 通话时长 {dur_str}', 'duration': dur_sec}

    # Pattern 3: Direct duration element
    dur_el = root.find('.//duration')
    if dur_el is not None and dur_el.text:
        try:
            sec = int(dur_el.text.strip())
            dur_str = f'{sec // 60}:{sec % 60:02d}'
            return {'text': f'[网络电话] 通话时长 {dur_str}', 'duration': sec}
        except ValueError:
            pass

    return {'text': '[网络电话]'}



@register(10000)
def parse_system_wx4(xml_bytes):
    """Parse WeChat 4.x system messages (type 10000) from zstd-decompressed XML.

    Handles: revokemsg, delchatroommember, mmchatroomtopmsg, paymsg, patmsg.
    """
    root = _safe_xml(xml_bytes)
    if root is None:
        return {'text': '[系统消息]'}

    sysmsg = root.find('.//sysmsg')
    if sysmsg is None:
        sysmsg = root if root.tag == 'sysmsg' else None

    if sysmsg is None:
        # Fallback: try regex for corrupted XML
        xx = _wx4_re_extract(xml_bytes, {
            'content': r'<content>(.*?)</',
            'replacemsg': r'<replacemsg>(.*?)</',
            'tips': r'<tips>(.*?)</',
        })
        if xx:
            text = xx.get('content') or xx.get('replacemsg') or xx.get('tips') or ''
            cdata = xx.get('_cdata', '')
            if cdata:
                text = cdata
            if text.strip():
                return {'text': f'[系统] {text.strip()}', 'sysmsg_type': 'generic'}
        return {'text': None}

    stype = sysmsg.get('type', '')

    # --- revokemsg: message revoked ---
    if stype == 'revokemsg':
        rev = sysmsg.find('revokemsg')
        if rev is not None:
            content = rev.findtext('content', '')
            revoketime = rev.findtext('revoketime', '0')
            if content:
                return {
                    'text': f'[系统] {content}',
                    'sysmsg_type': 'revoke',
                    'revoke_content': content,
                    'revoke_time': int(revoketime) if revoketime and revoketime.isdigit() else 0,
                }
        return {'text': '[系统] 撤回了一条消息', 'sysmsg_type': 'revoke'}

    # --- delchatroommember: group member added/removed ---
    if stype == 'delchatroommember':
        dcm = sysmsg.find('delchatroommember')
        if dcm is not None:
            plain = dcm.findtext('plain', '')
            link = dcm.find('link')
            scene = link.findtext('scene', '') if link is not None else ''
            memberlist = link.find('memberlist') if link is not None else None
            usernames = []
            if memberlist is not None:
                for u in memberlist.findall('username'):
                    usernames.append(u.text or '')
            display = (plain or '').strip()
            if not display:
                display = (dcm.findtext('text', '') or '').strip()
            return {
                'text': f'[系统] {display}' if display else '[系统] 群成员变更',
                'sysmsg_type': 'group_member',
                'scene': scene,
                'usernames': usernames,
                'display_text': display,
            }
        return {'text': '[系统] 群成员变更', 'sysmsg_type': 'group_member'}

    # --- mmchatroomtopmsg: pin/unpin group message ---
    if stype == 'mmchatroomtopmsg':
        mm = sysmsg.find('mmchatroomtopmsg')
        if mm is not None:
            chatroom = mm.findtext('chatroomname', '')
            op = mm.findtext('op', '')
            nickname = mm.findtext('nickname', '')
            username = mm.findtext('username', '')
            action_map = {'1': '置顶了一条消息', '2': '移除了一条置顶消息'}
            action = action_map.get(op, '操作了置顶消息')
            who = nickname or username or '有人'
            return {
                'text': f'[系统] {who}{action}',
                'sysmsg_type': 'pin_msg',
                'chatroom': chatroom,
                'op': op,
                'operator': who,
                'operator_username': username,
            }
        return {'text': '[系统] 群置顶消息变更', 'sysmsg_type': 'pin_msg'}

    # --- paymsg: payment-related messages ---
    if stype == 'paymsg':
        content_el = sysmsg.find('content')
        if content_el is not None and content_el.text:
            display = content_el.text.strip()
            clean = re.sub(r'</?_wc_custom_link_[^>]*>', '', display)
            link_match = re.search(r'href="(weixin://[^"]*)"', display)
            return {
                'text': f'[系统] {clean}',
                'sysmsg_type': 'payment',
                'pay_content': clean,
                'pay_link': link_match.group(1) if link_match else None,
                'raw_content': display,
            }
        return {'text': '[系统] 支付消息', 'sysmsg_type': 'payment'}

    # --- patmsg: nudge (拍一拍) ---
    if stype == 'patmsg':
        pat = sysmsg.find('patmsg')
        if pat is not None:
            from_user = pat.findtext('fromusername', '')
            to_user = pat.findtext('tousername', '')
            display = f'"{from_user}" 拍了拍 "{to_user}"'
            return {
                'text': f'[系统] {display}',
                'sysmsg_type': 'pat',
                'from_user': from_user,
                'to_user': to_user,
            }
        return {'text': '[拍一拍]', 'sysmsg_type': 'pat'}

    # --- Generic fallback ---
    for tag in ('content', 'replacemsg', 'tips', 'plain', 'text'):
        el = sysmsg.find(tag)
        if el is not None and el.text and el.text.strip():
            return {'text': f'[系统] {el.text.strip()}', 'sysmsg_type': stype or 'generic'}

    for el in sysmsg.iter():
        if el.text and el.text.strip():
            return {'text': f'[系统] {el.text.strip()}', 'sysmsg_type': stype or 'generic'}

    return {'text': '[系统消息]', 'sysmsg_type': stype or 'generic'}


@register(10002)
def parse_system(xml_bytes):
    """Parse system message XML (WeChat 3.x) or return None for plain text."""
    root = _safe_xml(xml_bytes)
    if root is not None:
        for tag in ('content', 'replacemsg', 'tips', 'sysmsg'):
            text = root.findtext(f'.//{tag}')
            if text and text.strip():
                return {'text': f'[系统] {text.strip()}'}
        return {'text': None}

    # WeChat 4.x: XML corrupted by protobuf tags — use regex extraction
    xx = _wx4_re_extract(xml_bytes, {
        'content': r'<content>(.*?)</',
        'replacemsg': r'<replacemsg>(.*?)</',
        'tips': r'<tips>(.*?)</',
    })
    if not xx:
        return {'text': None}

    text = xx.get('content') or xx.get('replacemsg') or xx.get('tips') or ''
    if text.strip():
        return {'text': f'[系统] {text.strip()}'}
    return {'text': None}
