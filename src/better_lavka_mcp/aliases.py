"""Short ids for the model: three common words instead of a 44-character hex id.

`7acc5006e3dd4636bb613646099df8a5000400020001` costs ~23 tokens; `amber fox wave` costs ~5.
The words come from a hash of the real id, so the same product always gets the same short id.
Every short id the server has shown is kept in a small owner-only file, so it can be turned back
into the real id in later calls and sessions; a hash collision just tries the next variant.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

PATH = Path.home() / '.local/share/yandex-lavka-mcp/aliases.json'
LIMIT = 20000  # oldest short ids are forgotten beyond this

# Each word is a single token on its own and after a space in common LLM tokenizers.
WORDS = (
    'amber arch arrow ash atlas axe badge banner barn beam bear bee bell belt bench bird blade blue boat bolt bone '
    'book boot bow box brick bridge brook brush bud bull camel camp cape cargo castle cat chain chalk chart chief '
    'circle city clock cloud coin cord crest crow cube curve delta dew dial diamond dock dog door dragon earth echo '
    'edge elk elm ember engine fan farm fern field film fir fire flag fog forest fox frame gate gem ghost glass gold '
    'grass green hall hammer hare hat heart helm hill horn horse hut ice iris iron ivy jade jet key king kite lake '
    'lamp laser leaf lens lever light lion lock loom mask mesa metal mirror mist moon mouse nest net night north '
    'nova oak orbit owl paper path peak pen pilot pine pipe planet pond pony port quest rabbit rain ram reef ridge '
    'ring river road rock rocket roof root rope rose ruby sand scale scroll sea seal shade shell shield ship shore '
    'silver sky sled snow solar spark sphere spring square star steam steel stone storm stream sun tank thorn torch '
    'tower trail tree vault wagon wall wand wave wheel wind wing winter wolf wood yak bay plain glyph gong'
).split()
_WORD_SET = frozenset(WORDS)

_table = None  # short id -> real id
_reverse = None  # real id -> short id
_dirty = False


def _load():
    global _table, _reverse
    if _table is None:
        try:
            data = json.loads(Path(PATH).read_text(encoding='utf-8'))
            _table = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _table = {}
        _reverse = {v: k for k, v in _table.items()}
    return _table


def _candidate(real_id, attempt):
    digest = hashlib.sha256(f'{real_id}#{attempt}'.encode()).digest()
    return ' '.join(WORDS[int.from_bytes(digest[i:i + 2], 'big') % len(WORDS)] for i in (0, 2, 4))


def short(real_id):
    """Short id for a real id (remembered until save())."""
    global _dirty
    if not isinstance(real_id, str) or not real_id:
        return real_id
    table = _load()
    if real_id in _reverse:
        return _reverse[real_id]
    attempt = 0
    while (alias := _candidate(real_id, attempt)) in table:
        attempt += 1
    table[alias] = real_id
    _reverse[real_id] = alias
    _dirty = True
    return alias


def is_alias(value):
    words = value.split() if isinstance(value, str) else []
    return len(words) == 3 and all(w in _WORD_SET for w in words)


def resolve(value):
    """Real id for a short id; real ids pass through unchanged."""
    if not is_alias(value):
        return value
    real = _load().get(' '.join(value.split()))
    if real is None:
        raise ValueError(f'Unknown id "{value}". Search or list again to get a current id.')
    return real


def save():
    """Write new short ids to disk (owner-only), keeping the newest LIMIT."""
    global _dirty, _table, _reverse
    if not _dirty:
        return
    table = _load()
    if len(table) > LIMIT:
        _table = dict(list(table.items())[-LIMIT:])
        _reverse = {v: k for k, v in _table.items()}
    target = Path(PATH)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.lavka-aliases-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(_table, stream, ensure_ascii=False)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    _dirty = False


def reset():
    """Forget the in-memory table (tests, or after PATH changes)."""
    global _table, _reverse, _dirty
    _table, _reverse, _dirty = None, None, False
