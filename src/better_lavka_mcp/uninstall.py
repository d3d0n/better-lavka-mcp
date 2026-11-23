"""`better-lavka-mcp uninstall`: delete everything the server stored on this computer.

That is the Lavka session (sign-in cookies), remembered address and card, short ids, order
previews and the checkout/cancellation guards. The program itself and the client entry are
removed separately; the command prints how.
"""
import os
import shutil
import sys
from pathlib import Path

STATE_DIR = Path.home() / '.local/share/yandex-lavka-mcp'


def targets():
    """Files and folders the server keeps, existing ones only."""
    found = [STATE_DIR] if STATE_DIR.exists() else []
    session = os.environ.get('YANDEX_SESSION_FILE')
    if session:
        path = Path(session).expanduser()
        if path.exists() and STATE_DIR not in path.parents:
            found.append(path)
    return found


def run(argv):
    yes = '--yes' in argv or '-y' in argv
    found = targets()
    if not found:
        print('Nothing to delete: no Better Lavka data on this computer.')
    else:
        print('This deletes the Lavka sign-in, remembered address and card, short ids, order previews and checkout guards:')
        for path in found:
            print(f'  {path}')
        if not yes:
            if not sys.stdin.isatty():
                print('Run again with --yes to delete.')
                return 1
            if input('Delete? [y/N] ').strip().lower() not in ('y', 'yes'):
                print('Nothing deleted.')
                return 1
        for path in found:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        print('Deleted.')
    print('\nTo remove the program as well:')
    print('  claude mcp remove lavka          # or delete the "lavka" entry from your MCP client config')
    print('  uv cache clean better-lavka-mcp  # if you ran it with uvx')
    print('  uv tool uninstall better-lavka-mcp  # if you installed it with uv tool install')
    print('Deleting the files signs this computer out; to end the session on Yandex too, use "Sign out of all devices" in Yandex ID.')
    return 0
