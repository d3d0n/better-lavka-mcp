"""Public runtime must not depend on excluded experiment scripts."""
import subprocess
import sys


def test_order_module_imports_without_experiment():
    code = '''
import sys
sys.modules["authorized_experiment"] = None
from better_lavka_mcp.orders import submit as orders
assert orders.GUARD.name == "fee-experiment-20261029.sqlite"
'''
    subprocess.run([sys.executable, "-c", code], check=True)
