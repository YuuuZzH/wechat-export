import os
import sys

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJ = os.path.dirname(_SRC)

sys.path.insert(0, _SRC)
from web.app import run_server
run_server(os.path.join(_PROJ, 'output', 'decrypted'))
