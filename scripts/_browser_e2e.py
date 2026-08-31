"""Workstream D — full browser flow against the REAL running stack.

Path exercised (headless Chromium, dev server :5173, merchant :8000, buyer
:8001):
  1. human types a prompt -> buyer agent discovers + builds an order
  2. agent presents the bill -> human approves
  3. browser signs a UserIntentAssertion with its own device key (tweetnacl,
     localStorage) and registers the public key -> decision streams to agent
  4. merchant creates a REAL Razorpay test-mode order -> PaymentCard renders
  5. human pays with the Razorpay test card 4111 1111 1111 1111 (any expiry,
     CVV, OTP) inside Razorpay's real Checkout modal
  6. payment_callback -> backend verifies -> "Paid" authoritative UI
  7. evidence: JSON of completed session + provenance + consumer projection

Run:  python scripts/_browser_e2e.py          (needs all three servers up)
Output evidence under artifacts/e2e/.
"""
import json
import os
import re
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
EVID = "artifacts/e2e"
PROMPT = "Buy milk, bread and eggs for under 300"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def snap(page, name: str) -> None:
    os.makedirs(EVID, exist_ok=True)
    page.screenshot(path=f"{EVID}/{name}.png")
    log(f"  screenshot -> {EVID}/{name}.png")


def dump_dom(page, tag: str) -> None:
    os.makedirs(EVID, exist_ok=True)
    with open(f"{EVID}/dom_{tag}.txt", "w", encoding="utf-8") as fh:
        fh.write(page.locator("body").inner_text())
    log(f"  dom dump -> {EVID}/dom_{tag}.txt")


def reach_bill(page) -> str:
    """Type the prompt, handle any substitution, wait for the bill bubble."""
    ta = page.locator('textarea[aria-label="Message your shopping assistant"]')
    ta.wait_for(state="visible", timeout=20000)
    ta.fill(PROMPT)
    ta.press("Enter")
    log("prompt submitted -> agent flow started")

    deadline = time.time() + 200
    while time.time() < deadline:
        swap = page.locator(".swap-bubble").first
        if swap.count() > 0 and swap.locator("button.qr-chip.primary").count() > 0:
            log("substitution offer detected -> take it")
            swap.locator("button.qr-chip.primary").first.click(timeout=5000)
            continue
        order = page.locator(".order-bubble").first
        if order.count() > 0 and order.locator("button.qr-chip.primary").count() > 0:
            total_text = order.locator(".total-line span").last.inner_text()
            log(f"bill presented -> total {total_text}")
            snap(page, "02_bill")
            return total_text
        if page.locator(".alert-line").count() > 0:
            dump_dom(page, "error")
            raise RuntimeError("error alert in chat: " + page.locator(".alert-line").first.inner_text())
        page.wait_for_timeout(1500)
    dump_dom(page, "no_bill")
    raise TimeoutError("bill never presented; dom dumped for diagnosis")


def approve_bill(page) -> None:
    order = page.locator(".order-bubble").first
    order.scroll_into_view_if_needed()
    order.locator("button.qr-chip.primary").first.click(timeout=8000)
    log("approve clicked -> browser now signs a user intent assertion")


def pay_with_razorpay(page) -> None:
    """Drive Razorpay's REAL Checkout modal with the test card."""
    snap(page, "04_razorpay_land")
    frame = None
    deadline = time.time() + 60
    while time.time() < deadline:
        for f in page.frames:
            if "razorpay.com" in f.url or "rzp.io" in f.url or "px-cloud.net" in f.url:
                frame = f
                break
        if frame and frame.url.startswith("http"):
            iframe_url = frame.url
            log(f"Razorpay frame found -> {iframe_url[:90]}")
            break
        if frame is None:
            page.wait_for_timeout(1000)
    if frame is None:
        snap(page, "04_no_iframe")
        raise TimeoutError("no Razorpay frame appeared (page.frames=" + str([f.url for f in page.frames]) + ")")

    page.wait_for_timeout(3500)
    snap(page, "04b_razorpay_iframe")
    try:
        body_text = frame.locator("body").inner_text(timeout=5000)
        with open(f"{EVID}/razorpay_frame_body.txt", "w", encoding="utf-8") as fh:
            fh.write(body_text)
    except Exception:
        pass

    # New Razorpay v1 checkout shows mandate/save-card consent; click through it.
    for label in ("Continue", "Add a new card"):
        try:
            btn = frame.locator(f'button:has-text("{label}")').first
            if btn.count() > 0:
                btn.click(timeout=4000)
                log(f"consent step clicked -> {label}")
                page.wait_for_timeout(1500)
                break
        except Exception:
            continue

    snap(page, "04c_razorpay_card")

    card = frame.locator(
        'input[name="card[number]"], input[name="card.number"], input[placeholder*="Card number"], input[data-testid="cardNumber"], input[autocomplete="cc-number"]'
    ).first
    try:
        card.wait_for(state="visible", timeout=45000)
    except Exception:
        dump = frame.locator("body").inner_text(timeout=5000)
        try:
            fields = frame.evaluate(
                """() => Array.from(document.querySelectorAll('input,button'))
                    .map(el => ({t: el.type||'', id: el.id||'', n: el.name||'',
                                  p: el.placeholder||'', tx: (el.tagName==='BUTTON'? el.innerText : '').trim().slice(0,40)}))"""
            )
        except Exception:
            fields = []
        with open(f"{EVID}/razorpay_inputs.json", "w", encoding="utf-8") as fh:
            json.dump({"frame": frame.url, "body": dump[:1500], "fields": fields}, fh, indent=2)
        raise RuntimeError("card field not found; inspect artifacts/e2e/razorpay_inputs.json")

    try:
        frame.locator('input[name="save"]').first.check(timeout=4000)
    except Exception:
        pass
    card.fill("4111 1111 1111 1111")
    frame.locator(
        'input[name="card[expiry]"], input[name="card.expiry"], input[placeholder*="MM/YY"], input[data-testid="cardExpiry"], input[autocomplete="cc-exp"]'
    ).first.fill("12/34")
    frame.locator(
        'input[name="card[cvv]"], input[name="card.cvv"], input[placeholder*="CVV"], input[data-testid="cardCvv"], input[autocomplete="cc-csc"]'
    ).first.fill("123")

    cont = frame.locator('button[type="submit"]:has-text("Continue"), button:has-text("Pay")')
    clicked = False
    for label in ("Pay", "Continue", "Verify"):
        try:
            b = frame.locator(f'button:visible:has-text("{label}")')
            if b.count() > 0:
                b.first.click(timeout=6000)
                clicked = True
                log(f"card submit clicked (visible, text={label})")
                break
        except Exception:
            continue
    if not clicked:
        try:
            frame.locator('button[type="submit"]:visible').first.click(timeout=6000)
            clicked = True
            log("card submit clicked (visible submit)")
        except Exception:
            pass
    if not clicked:
        raised_from = frame.evaluate(
            """() => { const btns = Array.from(document.querySelectorAll('button'))
                .filter(b => b.offsetParent !== null);
                if (!btns.length) return {ok:false, why:'no visible buttons'};
                const target = btns[btns.length-1];
                target.click(); return {ok:true, clicked: target.innerText.slice(0,40)||target.type}; }"""
        )
        if not raised_from["ok"]:
            with open(f"{EVID}/razorpay_submit_fail.json", "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "why": raised_from,
                        "frame": frame.url,
                        "frames": [f.url for f in page.frames],
                        "body": frame.locator("body").inner_text(timeout=5000)[:2000],
                    },
                    fh,
                    indent=2,
                )
            raise RuntimeError("no visible submit after card fill; inspect razorpay_submit_fail.json")
        log(f"card submit clicked (JS fallback -> {raised_from['clicked']})")
    log("card submitted -> awaiting OTP screen")
    snap(page, "05_after_card_pay")
    page.wait_for_timeout(4500)

    otp_sel = 'input[name="otp"], input[autocomplete="one-time-code"], input[placeholder*="OTP"], input[placeholder*="otp"]'
    otp_frame = _wait_for_otp(page, otp_sel)
    if otp_frame is None:
        snap(page, "05_no_otp")
        info = _otp_diagnostic(page)
        with open(f"{EVID}/razorpay_otp_fail.json", "w", encoding="utf-8") as fh:
            json.dump(
                {"pages": [p.url for p in page.context.pages],
                 "frames": [f.url for f in page.frames],
                 "info": info},
                fh, indent=2,
            )
        raise RuntimeError("no OTP field found after card payment; inspect razorpay_otp_fail.json")
    snap(page, "06_otp")
    inp = otp_frame.locator(otp_sel).first
    maxlen = inp.get_attribute("maxlength")
    inp.fill("123456" if maxlen in (None, "6", "5", "4") else "1234")
    for label in ("Verify", "Pay", "Pay Amount", "Confirm", "Submit"):
        try:
            b = otp_frame.locator(f'button:visible:has-text("{label}")')
            if b.count() > 0:
                b.first.click(timeout=6000)
                log(f"OTP submitted via button ({label})")
                otp_frame.locator("body").inner_text(timeout=3000)
                break
        except Exception:
            continue
    else:
        otp_frame.evaluate(
            """() => { const btns = Array.from(document.querySelectorAll('button'))
                .filter(b => b.offsetParent !== null);
                if (btns.length) btns[btns.length-1].click(); }"""
        )
        log("OTP submitted (JS fallback)")
    log("OTP submitted -> awaiting backend verification")


def _wait_for_otp(page, otp_sel, total=200):
    """Poll all pages/frames for an OTP field. If Razorpay's hCaptcha
    challenge is blocking the card POST (headless/automation), request one
    human interaction in the visible Chrome window, then keep polling."""
    announced = False
    deadline = time.time() + total
    last = time.time()
    while time.time() < deadline:
        for pg in [page, *page.context.pages]:
            try:
                for f in pg.frames:
                    try:
                        if f.locator(otp_sel).count() > 0:
                            return f
                    except Exception:
                        continue
            except Exception:
                continue
        challenge = any(
            "hcaptcha.com/captcha/" in f.url or "hcaptcha.com" in f.url
            for f in page.frames
        )
        if challenge and not announced:
            announced = True
            log(
                "RAZORPAY hCaptcha is gating the card step (common for automated browsers). "
                "Complete the human-check in the Chrome window that opened; the script continues "
                "automatically once the OTP prompt appears."
            )
        if time.time() - last > 10:
            snap(page, "otp_waiting")
            last = time.time()
        page.wait_for_timeout(3000)
    return None


def _find_otp_frame(page, current, otp_sel, settle=5.0):
    page.wait_for_timeout(int(settle * 1000))
    checked = set()
    for _ in range(3):
        for pg in [page, *page.context.pages]:
            try:
                for f in pg.frames:
                    if f in checked:
                        continue
                    checked.add(f)
                    try:
                        if f.locator(otp_sel).count() > 0:
                            return f
                    except Exception:
                        continue
            except Exception:
                continue
        page.wait_for_timeout(2000)
    return None


def _otp_diagnostic(page):
    out = []
    for pg in [page, *page.context.pages]:
        for f in pg.frames:
            try:
                body = f.locator("body").inner_text(timeout=1500)
                out.append({"frame": f.url[:80], "body": body[:600]})
            except Exception:
                pass
    return out


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
        console_log = []
        req_log = []
        page.on("console", lambda m: console_log.append(f"{m.type}: {m.text}"))
        page.on("request", lambda r: req_log.append(f"{r.method} {r.url}") if "/api/" in r.url else None)
        page.on("requestfailed", lambda r: console_log.append(f"REQFAIL {r.method} {r.url} {r.failure}"))
        page.on("response", lambda r: req_log.append(f"RESP {r.status} {r.url}") if "/api/" in r.url else None)
        try:
            page.goto(BASE, wait_until="networkidle", timeout=45000)
            snap(page, "01_home")
            total_text = reach_bill(page)
            approve_bill(page)

            pay_btn = page.locator("button.btn-pay").first
            pay_btn.wait_for(state="visible", timeout=120000)
            pay_btn.scroll_into_view_if_needed()
            snap(page, "03_payment_card")
            pay_btn.click(timeout=10000)
            log("Pay clicked -> Razorpay modal should open")

            pay_with_razorpay(page)

            paid = page.locator(".pay-paid")
            paid.wait_for(state="visible", timeout=150000)
            paid_text = paid.inner_text()
            log(f"AUTHORITATIVE PAID STATE -> {paid_text}")
            snap(page, "07_paid")
        except Exception:
            with open(f"{EVID}/console.log.txt", "w", encoding="utf-8") as fh:
                fh.write("\n".join(console_log))
            with open(f"{EVID}/requests.log.txt", "w", encoding="utf-8") as fh:
                fh.write("\n".join(req_log))
            do_dump = page.locator("body").inner_text()
            with open(f"{EVID}/dom_fail.txt", "w", encoding="utf-8") as fh:
                fh.write(do_dump)
            log("console + requests dumped under artifacts/e2e/ (failed path)")
            raise
        finally:
            page.context.close()

        amount = re.sub(r"[^0-9.]", "", paid_text.split("₹", 1)[-1])
        with open(f"{EVID}/flow.json", "w", encoding="utf-8") as fh:
            json.dump({"flow": "workstream_d_fully_signed_payment", "ui_total_paid": amount}, fh, indent=2)
        print(f"\nUI PAID -> {paid_text} ; bill total said {total_text}")


if __name__ == "__main__":
    main()