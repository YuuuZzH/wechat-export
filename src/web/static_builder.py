"""Generate static offline-viewable chat archive."""
import json
import os
import shutil
from pathlib import Path


def build_static(decrypted_dir: str, output_dir: str, wxid: str = None) -> str:
    """Generate a static HTML/JS chat viewer archive.

    Steps:
    1. Copy web/static/ (JS, CSS) to output/static/
    2. For each chat, fetch messages via query_messages and write JSON to static/data/
    3. Copy web/templates/index.html as static/index.html
    4. Copy media/ directory if present

    Args:
        decrypted_dir: Path to decrypted data (data/raw/ or decrypted/)
        output_dir: Backup output root (contains data/ and media/)

    Returns:
        Path to the generated static directory.
    """
    static_dir = os.path.join(output_dir, 'static')
    data_dir = os.path.join(static_dir, 'data')
    js_dir = os.path.join(static_dir, 'js')
    css_dir = os.path.join(static_dir, 'css')
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(js_dir, exist_ok=True)
    os.makedirs(css_dir, exist_ok=True)

    # Copy static assets
    web_static = os.path.join(os.path.dirname(__file__), 'static')
    if os.path.isdir(os.path.join(web_static, 'js')):
        for f in os.listdir(os.path.join(web_static, 'js')):
            shutil.copy2(os.path.join(web_static, 'js', f),
                        os.path.join(js_dir, f))
    if os.path.isdir(os.path.join(web_static, 'css')):
        for f in os.listdir(os.path.join(web_static, 'css')):
            shutil.copy2(os.path.join(web_static, 'css', f),
                        os.path.join(css_dir, f))

    # Pre-render chat data as JSON
    from engine.services.chat import get_contacts
    from engine.services.message import query_messages

    contacts = get_contacts(decrypted_dir, wxid)
    for contact in contacts[:200]:  # Limit for performance
        chat_id = contact['id']
        try:
            result = query_messages(decrypted_dir, chat_id, wxid=wxid,
                                    page=1, per_page=200)
            out_path = os.path.join(data_dir, f"{chat_id}.json")
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, default=str)
        except Exception:
            continue

    # Write contacts index
    with open(os.path.join(data_dir, '_contacts.json'), 'w', encoding='utf-8') as f:
        json.dump({'contacts': contacts}, f, ensure_ascii=False, default=str)

    # Copy HTML template
    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'index.html')
    if os.path.isfile(template_path):
        shutil.copy2(template_path, os.path.join(static_dir, 'index.html'))

    # Create start.bat
    bat_path = os.path.join(output_dir, 'start.bat')
    with open(bat_path, 'w', encoding='utf-8') as f:
        f.write('@echo off\n')
        f.write(f'start "" "{static_dir}/index.html"\n')
        f.write('echo Chat viewer opened in browser.\n')
        f.write('pause\n')

    return static_dir
