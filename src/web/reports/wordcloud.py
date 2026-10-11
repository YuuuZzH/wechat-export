"""Word cloud report route."""
from . import reports_bp
from flask import request, jsonify, current_app


@reports_bp.route('/wordcloud')
def wordcloud():
    """Generate word cloud data for a chat.

    Query params: chat_id (required), limit (default 100)
    Returns: [{word, weight}, ...]
    """
    chat_id = request.args.get('chat_id', '')
    if not chat_id:
        return jsonify({'error': 'chat_id required'}), 400

    decrypted_dir = current_app.config.get('DECRYPTED_DIR', '')
    limit = request.args.get('limit', 100, type=int)

    try:
        words = _generate_wordcloud(decrypted_dir, chat_id, limit)
        return jsonify({'words': words, 'chat_id': chat_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _generate_wordcloud(decrypted_dir: str, chat_id: str, limit: int = 100) -> list:
    """Extract word frequencies from messages.

    Ported from src/wordcloud_gen.py logic. Queries messages via the
    engine services layer, tokenizes with jieba, filters stopwords.
    """
    from engine.services.message import query_messages

    result = query_messages(decrypted_dir, chat_id, page=1, per_page=5000)
    messages = result.get('messages', [])

    texts = []
    for msg in messages:
        content = msg.get('content', '')
        if isinstance(content, str) and len(content) > 1:
            texts.append(content)

    if not texts:
        return []

    try:
        import jieba
    except ImportError:
        from collections import Counter
        all_chars = ''.join(texts)
        counter = Counter(all_chars)
        return [{'word': c, 'weight': n} for c, n in counter.most_common(limit)]

    from stopwords import is_meaningful, filter_words
    from collections import Counter

    all_words = []
    for text in texts:
        words = jieba.lcut(text)
        all_words.extend(filter_words(words))

    counter = Counter(all_words)
    return [{'word': w, 'weight': n}
            for w, n in counter.most_common(limit)
            if is_meaningful(w)]
