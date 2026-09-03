"""Drive the REAL Razorpay modal from the dashboard Ops console for a specific
checkout session (created deterministically via scripts/_recur_order_api.py).

Human interaction: when Razorpay's hCaptcha appears in the open Chrome window,
solve it; the script auto-fills the test card and OTP.
"""
import json
import os
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

sys.path.insert(0, r"D:\Hackathons\rz-agent")

from scripts._browser_e2e import log, snap, pay_with_razorpay, dump_dom, EVID  # noqa: E402

BASE = "http://localhost:5173"
CSID = "cs_6ea61e6bd0"
OPS_PASS = "admin_demo_token"


def main() -> None:
    os.makedirs(EVID, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=EVID + "\\chrome_profile",
            channel="chrome",
            headless=False,
            slow_mo=55,
            viewport={"width": 1360, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE, wait_until="networkidle", timeout=45000)
        snap(page, "o1_home")

        page.evaluate("(csid) => window.dispatchEvent(new CustomEvent('acg:open-ops', {detail: csid}))", CSID)
        page.wait_for_timeout(1500)
        snap(page, "o2_ops_open")

        pwd = page.locator('input[type="password"]')
        if pwd.count() > 0:
            pwd.wait_for(state="visible", timeout=15000)
            pwd.fill(OPS_PASS)
            page.locator('button:has-text("Sign in")').first.click(timeout=8000)
            log("ops sign-in submitted")
            page.wait_for_timeout(2500)
            snap(page, "o3_ops_logged_in")

        pay_btn = page.locator("button.btn-pay").first
        pay_btn.wait_for(state="visible", timeout=60000)
        pay_btn.scroll_into_view_if_needed()
        text = pay_btn.inner_text()
        log(f"PaymentCard ready -> {text}")
        snap(page, "o4_payment_card")
        pay_btn.click(timeout=10000)
        log("Pay clicked -> Razorpay modal")

        pay_with_razorpay(page)
        log("card submitted; waiting for authoritative completed state (human handles any hCaptcha)")

        deadline = time.time() + 300
        last = time.time()
        while time.time() < deadline:
            if time.time() - last > 15:
                snap(page, "o5_waiting")
                last = time.time()
            try:
                import urllib.request

                with urllib.request.urlopen(f"http://localhost:8000/checkout_sessions/{CSID}", timeout=8) as r:
                    body = json.loads(r.read().decode())
                st = body.get("checkout_session", {}).get("status")
                if st in ("completed", "declined"):
                    log(f"AUTHORITATIVE STATE -> {st}")
                    snap(page, "o6_done")
                    break
            except Exception:
                pass
            page.wait_for_timeout(4000)

        page.context.close()


if __name__ == "__main__":
    main()