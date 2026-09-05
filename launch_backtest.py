"""Bootstrap: creates missing __init__.py and runs the backtest."""
import os
import subprocess
import sys

# Fix: create the missing root __init__.py
init_path = os.path.join("src", "betdoc", "__init__.py")
if not os.path.exists(init_path):
    with open(init_path, "w") as f:
        f.write("")
    print(f"Created missing {init_path}")
else:
    print(f"{init_path} already exists")

# Now run the actual backtest
print("Launching backtest...\n")
result = subprocess.run(
    [sys.executable, "run_backtest.py"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
)
sys.exit(result.returncode)
