"""Per-script non-secret settings written by the VPS mini app."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALID = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\.lua$', re.I)

def load_settings(script):
    name=Path(script).name
    if not VALID.fullmatch(name) or '..' in name: return {}
    directory=ROOT/'.lua-settings'
    path=directory/(name+'.json')
    if directory.is_symlink() or path.is_symlink(): return {}
    try:
        if path.stat().st_size>16384: return {}
        data=json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data,dict) and isinstance(data.get('values'),dict) and isinstance(data.get('enabled'),bool):
            if not data['enabled']: return {}
            data=data['values']
        if not isinstance(data,dict) or len(data)>64: return {}
        # Values are JSON data. The mini app limits size and nesting on write;
        # no code is executed and the Lua sandbox receives a JSON snapshot.
        return data
    except (OSError,ValueError,TypeError): return {}
