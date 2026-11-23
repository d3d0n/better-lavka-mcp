import subprocess
import sys


def test_qr_modules_import_in_fresh_interpreters():
    for name in ('better_lavka_mcp.auth.runtime', 'better_lavka_mcp.auth.qr',
                 'better_lavka_mcp.auth.tools', 'better_lavka_mcp.server'):
        result = subprocess.run([sys.executable, '-c', f'import {name}'], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
