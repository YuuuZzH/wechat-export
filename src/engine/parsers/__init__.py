"""Message XML parser registry. Importing types.py registers all parsers."""
PARSERS = {}

def register(msg_type: int):
    def decorator(fn):
        PARSERS[msg_type] = fn
        return fn
    return decorator

from engine.parsers import types  # triggers @register decorators
