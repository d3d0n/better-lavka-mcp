"""Short ids for the model: three common words instead of a 44-character hex id.

`7acc5006e3dd4636bb613646099df8a5000400020001` costs ~23 tokens; `amber fox wave` costs ~5.
The words come from a hash of the real id, so the same product always gets the same short id.
Issued short ids live in an owner-only SQLite file and are never reused for anything else:
both columns are UNIQUE, allocation runs in one transaction, so several server processes agree,
and a hash collision takes the next free variant.
"""
import hashlib
import sqlite3
from pathlib import Path

PATH = Path.home() / '.local/share/yandex-lavka-mcp/aliases.sqlite'

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


def _connect():
    target = Path(PATH)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(target, timeout=10, isolation_level=None)
    target.chmod(0o600)
    db.execute('CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, real_id TEXT NOT NULL UNIQUE)')
    return db


def _candidate(real_id, attempt):
    digest = hashlib.sha256(f'{real_id}#{attempt}'.encode()).digest()
    return ' '.join(WORDS[int.from_bytes(digest[i:i + 2], 'big') % len(WORDS)] for i in (0, 2, 4))


def short(real_id):
    """Short id for a real id; issued once and kept forever."""
    if not isinstance(real_id, str) or not real_id:
        return real_id
    db = _connect()
    try:
        row = db.execute('SELECT alias FROM aliases WHERE real_id = ?', (real_id,)).fetchone()
        if row:
            return row[0]
        db.execute('BEGIN IMMEDIATE')  # one allocator at a time across processes
        try:
            row = db.execute('SELECT alias FROM aliases WHERE real_id = ?', (real_id,)).fetchone()
            if row:
                db.execute('COMMIT')
                return row[0]
            attempt = 0
            while db.execute('SELECT 1 FROM aliases WHERE alias = ?', (alias := _candidate(real_id, attempt),)).fetchone():
                attempt += 1
            db.execute('INSERT INTO aliases (alias, real_id) VALUES (?, ?)', (alias, real_id))
            db.execute('COMMIT')
            return alias
        except BaseException:
            db.execute('ROLLBACK')
            raise
    finally:
        db.close()


def is_alias(value):
    words = value.split() if isinstance(value, str) else []
    return len(words) == 3 and all(w in _WORD_SET for w in words)


def resolve(value):
    """Real id for a short id; real ids pass through unchanged."""
    if not is_alias(value):
        return value
    db = _connect()
    try:
        row = db.execute('SELECT real_id FROM aliases WHERE alias = ?', (' '.join(value.split()),)).fetchone()
    finally:
        db.close()
    if row is None:
        raise ValueError(f'Unknown id "{value}". Search or list again to get a current id.')
    return row[0]
