"""Reset the demo to a pristine state: fresh DB, catalog, delegations.

Usage:
    python -m scripts.reset_demo
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"


def _stop_merchant_server() -> None:
    """Kill whatever process is listening on :8000 (the merchant server)."""
    import re
    import subprocess

    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids: set[str] = set()
    for line in out.splitlines():
        if ":8000" in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit() and parts[-1] != "0":
                pids.add(parts[-1])
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        print(f"  stopped merchant server (pid {pid})")


def main() -> None:
    print("Stopping merchant server on :8000…")
    _stop_merchant_server()
    time.sleep(1.5)

    removed = False
    for f in DATA.glob("merchant.db*"):
        try:
            f.unlink()
            removed = True
        except PermissionError:
            print(f"  could not remove {f.name} (in use)")

    from scripts.seed_catalog import main as seed_main

    seed_main()
    print("Demo reset complete. Start the stack:")
    print("  .venv\\Scripts\\python.exe -m uvicorn merchant_agent.main:app --port 8000")
    print("  .venv\\Scripts\\python.exe -m buyer_service.main")
    print("  cd dashboard && npm run dev")


if __name__ == "__main__":
    main()
