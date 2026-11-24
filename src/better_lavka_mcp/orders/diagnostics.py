"""A local, redacted record of what Lavka answered to each checkout, kept for diagnosing failed payments.

One JSON line per submit in checkout-diagnostics.jsonl next to the other state files, owner-only.
Only shapes and a fixed set of short status fields are kept: no card or wallet ids, no addresses,
no payment links, no tokens.
"""
import json
import os
import time

from better_lavka_mcp import paths

PATH = paths.data_dir() / 'checkout-diagnostics.jsonl'
# Scalar fields whose values are kept (truncated); every other value is reduced to its type.
KEEP = frozenset({'status', 'type', 'modalPlace', 'paymentType', 'needTrackPayment', 'code', 'message', 'errorCode',
                  'errorTitle', 'errorSubtitle', 'resolution', 'paymentStatus', 'title', 'subtitle', 'stage', 'trend'})


def redact(value, depth=0):
    """Keys everywhere; values only for KEEP fields; nothing below six levels."""
    if depth > 6:
        return '…'
    if isinstance(value, dict):
        return {str(k): (str(v)[:200] if k in KEEP and isinstance(v, (str, int, float, bool)) else redact(v, depth + 1))
                for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:10]]
    return type(value).__name__


def record(entry):
    """Append one entry; never raises, so diagnostics can't break a checkout."""
    try:
        paths.prepare_parent(PATH)
        fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if not paths.private_file(os.fstat(fd)):
                return None
            os.write(fd, (json.dumps({'recorded_at': time.time(), **entry}, ensure_ascii=False, sort_keys=True) + '\n').encode())
        finally:
            os.close(fd)
        return str(PATH)
    except (OSError, ValueError, TypeError):
        return None
