"""Register the subscription Mastercard test card (5104 0155 5555 5558) on the
buyer scope's saved method.

Reads the checkout session id created deterministically by scripts/_recur_order_api.py
(out of artifacts/recur_ready.json), opens the Ops console in a real Chrome window,
and drives the REAL Razorpay modal with the Mastercard number.

Human interaction: solve Razorpay's hCaptcha if it appears (script auto-fills card + OTP).
"""
import json
import os
import sys
import time as _time

from playwright.sync_api import sync_playwright

sys.path.insert(0, r"D:\Hackathons\rz-agent")

from scripts._browser_e2e import log, snap, pay_with_razorpay, EVID  # noqa: E402

BASE = "http://localhost:5173"
OPS_PASS = "admin_demo_token"
CARD_NUMBER = "5104 0155 5555 5558"


def main() -> None:
    with open(r"D:\Hackathons\rz-agent\artifacts\recur_ready.json", encoding="utf-8") as fh:
        csid = json.load(fh)["csid"]
    log(f"registering Mastercard on checkout {csid}")
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
        snap(page, "mc1_home")

        page.evaluate("(csid) => window.dispatchEvent(new CustomEvent('acg:open-ops', {detail: csid}))", csid)
        page.wait_for_timeout(1500)
        snap(page, "mc2_ops_open")

        pwd = page.locator('input[type="password"]')
        if pwd.count() > 0:
            pwd.wait_for(state="visible", timeout=15000)
            pwd.fill(OPS_PASS)
            page.locator('button:has-text("Sign in")').first.click(timeout=8000)
            log("ops sign-in submitted")
            page.wait_for_timeout(2500)
            snap(page, "mc3_ops_logged_in")

        pay_btn = page.locator("button.btn-pay").first
        pay_btn.wait_for(state="visible", timeout=60000)
        pay_btn.scroll_into_view_if_needed()
        log(f"PaymentCard ready -> {pay_btn.inner_text()}")
        snap(page, "mc4_payment_card")
        pay_btn.click(timeout=10000)
        log("Pay clicked -> Razorpay modal")

        pay_with_razorpay(page, card_number=CARD_NUMBER)
        log("Mastercard submitted; waiting for authoritative completed state (human handles any hCaptcha)")

        deadline = _time.time() + 300
        last = _time.time()
        while _time.time() < deadline:
            if _time.time() - last > 15:
                snap(page, "mc5_waiting")
                last = _time.time()
            try:
                import urllib.request

                with urllib.request.urlopen(f"http://localhost:8000/checkout_sessions/{csid}", timeout=8) as r:
                    body = json.loads(r.read().decode())
                st = body.get("checkout_session", {}).get("status")
                if st in ("completed", "declined"):
                    log(f"AUTHORITATIVE STATE -> {st}")
                    snap(page, "mc6_done")
                    break
            except Exception:
                pass
            page.wait_for_timeout(4000)

        page.context.close()


if __name__ == "__main__":
    main()