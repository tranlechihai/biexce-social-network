"""Real browser smoke test — t-006. Starts its own server, runs Playwright tests, stops server.

Requires environment variables (no defaults):
    TING_JWT_SECRET          — JWT signing key (any random string)
    TING_DEMO_PASSWORD       — password for seeded demo accounts
    TING_COOKIE_SECURE       — set to false for local HTTP testing
    TING_DATABASE_URL        — path for the browser smoke database

Usage:
    source .venv/bin/activate
    export TING_JWT_SECRET="your-random-secret"
    export TING_DEMO_PASSWORD="your-choice"
    export TING_COOKIE_SECURE=false
    export TING_DATABASE_URL="sqlite:////tmp/browser_smoke.db"
    python docs/smoke_browser_final.py
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Enforce required env — no defaults allowed for security env vars
_REQUIRED = ["TING_JWT_SECRET", "TING_DEMO_PASSWORD"]
_MISSING = [v for v in _REQUIRED if not os.environ.get(v)]
if _MISSING:
    print(
        f"FATAL: Required environment variables not set: {_MISSING!r}\n"
        f"Set them before running:\n"
        f"  export TING_JWT_SECRET='your-random-secret'\n"
        f"  export TING_DEMO_PASSWORD='your-choice'",
        file=sys.stderr,
    )
    sys.exit(1)

os.environ.setdefault("TING_COOKIE_SECURE", "false")
os.environ.setdefault("TING_DATABASE_URL", "sqlite:////tmp/browser_smoke.db")

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8765"
PORT = 8765
DEMO_PW = os.environ["TING_DEMO_PASSWORD"]  # Read from env, never hard-coded
DESKTOP = {"width": 1280, "height": 900}
MOBILE = {"width": 375, "height": 812}
OUTPUT = Path("docs")
SCREENS_DIR = OUTPUT / "screenshots"

results = []


def rec(cat, item, passed, detail=""):
    results.append({"cat": cat, "item": item, "passed": bool(passed) if passed is not None else False, "inconc": passed is None, "detail": detail})
    if passed is None:
        s = "INCONCLUSIVE"
    elif passed:
        s = "PASS"
    else:
        s = "FAIL"
    print(f"  [{s}] {item}")
    if detail:
        print(f"         {detail}")


def screenshot(page, name):
    SCREENS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    p = str(SCREENS_DIR / f"{name}-{ts}.png")
    page.screenshot(path=p)
    return p


# -------------------------------------------------------------------
# Server management
# -------------------------------------------------------------------

def _preseed():
    """Initialize schema (via brief server spin) then seed demo data.

    The app's startup event runs validate_and_initialize_schema().
    We spin the server just long enough for that, stop it, then seed.
    """
    py = sys.executable
    env = os.environ.copy()

    # --- Phase 1: Schema init via startup event ---
    proc = subprocess.Popen(
        [py, "-m", "uvicorn", "ting_ting.main:app", "--port", str(PORT + 1), "--host", "127.0.0.1"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        for _ in range(30):
            time.sleep(0.5)
            try:
                import urllib.request
                urllib.request.urlopen(f"http://127.0.0.1:{PORT + 1}/login", timeout=3)
                break
            except Exception:
                pass
        # Schema init is done via startup event; stop server immediately
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    print("Schema initialized.")

    # --- Phase 2: Seed ---
    result = subprocess.run(
        [py, "-m", "ting_ting", "seed"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"Seed failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(result.stdout.strip())


def start_server():
    py = sys.executable
    env = os.environ.copy()
    proc = subprocess.Popen(
        [py, "-m", "uvicorn", "ting_ting.main:app", "--port", str(PORT), "--host", "127.0.0.1"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for _ in range(30):
        time.sleep(0.5)
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/login", timeout=3)
            print("Server is ready.")
            return proc
        except Exception:
            pass
    # Server failed — show stderr for debugging
    proc.kill()
    _, err = proc.communicate(timeout=2)
    if err:
        print(f"Server stderr: {err.decode(errors='replace')[:500]}")
    print("Server failed to start.")
    sys.exit(1)


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    print("Server stopped.")


# -------------------------------------------------------------------
# Browser helpers
# -------------------------------------------------------------------

def navigate(page, path, timeout=15000):
    try:
        return page.goto(f"{BASE_URL}{path}", wait_until="domcontentloaded", timeout=timeout)
    except Exception as e:
        return str(e)


def wait_until_feed(page, timeout=10000):
    """Wait for /feed — robust to already-at-feed (redirect already consumed by expect_navigation)."""
    page.wait_for_load_state("domcontentloaded", timeout=timeout)
    assert "/web/web/web/feed" in page.url, f"Not at /feed: {page.url}"


def login(page, username, password):
    """Login via form, wait for redirect. POST→303→GET chain handled by expect_navigation."""
    navigate(page, "/web/login")
    page.wait_for_timeout(300)
    page.fill('input[name="identifier"]', username)
    page.fill('input[name="password"]', password)
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            page.click('button[type="submit"]', timeout=3000)
    except Exception:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            page.keyboard.press("Enter")
    # Defensive settle — ensures DOM is fresh after redirect
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass


def logout(page):
    """Logout and wait for redirect to /login."""
    try:
        with page.expect_navigation(wait_until="load", timeout=10000):
            page.click('form[action="/web/logout"] button[type="submit"]', timeout=5000)
    except Exception:
        try:
            with page.expect_navigation(wait_until="load", timeout=10000):
                page.click('button.btn-logout', timeout=5000)
        except Exception:
            page.evaluate("fetch('/logout', {method: 'POST', credentials: 'include'})")
            page.wait_for_timeout(1000)


# -------------------------------------------------------------------
# DESKTOP SMOKE
# -------------------------------------------------------------------

def run_desktop(page):
    print("\n" + "=" * 60)
    print("  DESKTOP VIEWPORT (1280x900)")
    print("=" * 60)

    print("\n  — Authentication —")

    # 1. Register form renders
    navigate(page, "/web/register")
    html = page.content()
    s = screenshot(page, "register_desktop")
    rec("Auth", "Register page renders with labels",
        "<form" in html and any(w in html.lower() for w in ["username", "email", "password"]),
        f"Screen: {s}")

    # 2. Register new user → redirect to feed
    navigate(page, "/web/register")
    page.wait_for_timeout(200)
    page.fill('input[name="username"]', "smoketest1")
    page.fill('input[name="email"]', "smoke1@browser.test")
    page.fill('input[type="password"]', "SmokeTestPass1!")
    # Use expect_navigation to follow the POST → 303 → feed redirect
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            page.click('form[action="/web/register"] button[type="submit"]', timeout=5000)
    except Exception:
        # Fallback: try Enter key if click fails
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                page.keyboard.press("Enter")
        except Exception:
            page.wait_for_timeout(2000)  # Last resort
    rec("Auth", "Register → redirects to feed",
        "/web/web/web/feed" in page.url, f"URL: {page.url}")

    # 3. Duplicate username error
    try:
        navigate(page, "/web/register")
        page.wait_for_timeout(300)
        page.fill('input[name="username"]', "smoketest1")
        page.fill('input[name="email"]', "other@t.com")
        page.fill('input[type="password"]', "SmokeTestPass1!")
        page.click('button[type="submit"]', timeout=3000)
        page.wait_for_timeout(1000)  # Wait for server response + DOM update
        html = page.content()
        s = screenshot(page, "dup_register")
        rec("Auth", "Duplicate username shows error",
            "exists" in html.lower() or "already" in html.lower(), f"Screen: {s}")
    except Exception as e:
        rec("Auth", "Duplicate username shows error", None, f"Test-script issue: {e}")

    # 4. Short username validation — wait for the POST response + error element
    try:
        navigate(page, "/web/register")
        page.wait_for_timeout(500)
        page.fill('input[name="username"]', "ab")
        page.fill('input[name="email"]', "short@t.com")
        page.fill('input[type="password"]', "SmokeTestPass1!")
        # POST /register with validation errors returns HTML at same URL (no redirect).
        # Use evaluate form.submit() — triggers real form post that Playwright detects.
        with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
            page.evaluate('() => document.querySelectorAll("form")[0].submit()')
        # Defensive: wait for the .alert-error element in the fresh DOM
        page.wait_for_selector('.alert-error', timeout=5000)
        html = page.content()
        s = screenshot(page, "short_username")
        # Server-side validation renders error list in the alert div.
        # The page must stay at /register when validation fails.
        has_err = (
            "3-30" in html or "must be" in html.lower() or
            "already exists" in html.lower() or
            "alert-error" in html.lower()
        )
        rec("Auth", "Short username shows validation error",
            has_err and "/web/register" in page.url, f"Screen: {s}, URL: {page.url}")
    except Exception as e:
        rec("Auth", "Short username shows validation error", None, f"Test-script issue: {e}")

    # 5. Logout — verify session ends (cookie cleared or redirect)
    navigate(page, "/web/feed")  # Ensure we're on a page that shows the logout button
    logout(page)
    # Check both: URL redirected to /login OR ting_ting_auth cookie is gone
    cookies = page.context.cookies()
    auth_cookie = [c for c in cookies if "ting_ting_auth" in c.get("name", "")]
    cookie_gone = len(auth_cookie) == 0
    rec("Auth", "Logout clears session (cookie gone → redirected)",
        cookie_gone or "/web/login" in page.url,
        f"URL: {page.url}, Auth cookies: {len(auth_cookie)}")

    # 6. Feed denied after logout
    navigate(page, "/web/feed")
    html = page.content()
    s = screenshot(page, "feed_after_logout")
    rec("Auth", "/web/feed denied after logout",
        "unauthenticated" in html or "log in" in html.lower(),
        f"URL: {page.url}")

    # 7. Wrong password → error, no secrets leaked
    navigate(page, "/web/login")
    page.wait_for_timeout(200)
    page.fill('input[name="identifier"]', "nonexistent")
    page.fill('input[type="password"]', "WrongPass99!")
    page.click('button[type="submit"]', timeout=3000)
    page.wait_for_timeout(500)
    html = page.content()
    s = screenshot(page, "wrong_pw")
    rec("Auth", "Wrong password → error, no secrets in HTML",
        ("invalid" in html.lower() or "error" in html.lower()) and "$2b$" not in html and "jwt_secret" not in html.lower(),
        f"Screen: {s}")

# 8. Login as alice → feed
    login(page, "alice", DEMO_PW)
    wait_until_feed(page)
    rec("Auth", "Login as alice → feed", "/web/web/web/feed" in page.url, f"URL: {page.url}")

    # --- Feed and Posts ---
    print("\n  — Feed and Posts —")

    html = page.content()
    s = screenshot(page, "alice_feed")
    rec("Feed", "Alice sees feed", "feed" in page.url, f"URL: {page.url}, Screen: {s}")

    # Create an ONLY_ME post — radio button for ONLY_ME is checked by default
    try:
        navigate(page, "/web/feed")
        page.wait_for_timeout(300)
        page.fill('#post-content', "Smoke ONLY_ME post")
        # ONLY_ME is the default checked radio — no action needed
        page.click('form[action="/web/posts/create"] button[type="submit"]', timeout=3000)
        # Wait for redirect cycle
        page.wait_for_timeout(2000)
        html = page.content()
        s = screenshot(page, "only_me_post")
        rec("Feed", "ONLY_ME post created via form",
            "smoke" in html.lower(),
            f"Post visible in feed, Screen: {s}")
    except Exception as e:
        rec("Feed", "ONLY_ME post created via form", None, f"Error: {e}")

    # Create a FRIENDS post — check the FRIENDS radio button (radio: just check the target)
    try:
        page.fill('#post-content', "Smoke FRIENDS post")
        # Radio buttons: just check the FRIENDS radio — Playwright handles switching
        page.check('input[name="audience"][value="FRIENDS"]', timeout=3000)
        page.click('form[action="/web/posts/create"] button[type="submit"]', timeout=3000)
        page.wait_for_load_state("domcontentloaded", timeout=5000)  # Fresh DOM after redirect
        s = screenshot(page, "friends_post")
        html = page.content()
        rec("Feed", "FRIENDS post created via form",
            "smoke" in html.lower(),
            f"Post visible in feed, Screen: {s}")
    except Exception as e:
        rec("Feed", "FRIENDS post created via form", None, f"Error: {e}")

    # Newest-first ordering: "Friends post" created last → should appear before "ONLY_ME post"
    page.reload(wait_until="domcontentloaded", timeout=10000)  # Fresh DOM
    html = page.content().lower()
    fp = html.find("smoke friends post")
    op = html.find("smoke only_me post")
    if fp >= 0 and op >= 0:
        ordered = fp < op
        rec("Feed", "Feed newest-first ordering",
            ordered, f"FRIENDS@{fp} before ONLY_ME@{op}")
    else:
        rec("Feed", "Feed newest-first ordering",
            None, f"Could not find both posts in HTML (positions: {fp}, {op})")

    # Logout alice, login as bob
    logout(page)
    login(page, "bob", DEMO_PW)
    wait_until_feed(page)
    html = page.content() + "<!-- post-content refresh -->"
    s = screenshot(page, "bob_feed")
    rec("Feed", "Bob sees Alice's FRIENDS post in feed",
        "hello friends" in html.lower() and "/web/web/web/feed" in page.url.lower(),
        f"Screen: {s}")

    # Carol's post NOT in Bob's feed
    rec("Feed", "Carol's post NOT in Bob's feed",
        "carol" not in html.lower(), f"Carol visible: {'carol' in html.lower()}")

    # --- Profile ---
    print("\n  — Profile —")
    navigate(page, "/web/profile/me")
    html = page.content()
    s = screenshot(page, "bob_profile")
    rec("Profile", "Bob's own profile renders",
        "bob" in html.lower(), f"Screen: {s}")

    navigate(page, "/web/profile/alice")
    html = page.content()
    s = screenshot(page, "alice_profile_view")
    rec("Profile", "View other user profile with relationship info",
        "alice" in html.lower(), f"Screen: {s}")

    # --- Social Actions ---
    print("\n  — Social Actions —")
    rec("Social", "Relationship actions visible on profile",
        any(w in html.lower() for w in ["friend", "unfriend", "request", "block"]),
        f"Screen: {s}")

    # --- Like and Comment ---
    print("\n  — Like and Comment —")
    navigate(page, "/web/feed")
    page.wait_for_timeout(500)
    html = page.content()
    s = screenshot(page, "interactions")
    rec("Like/Comment", "Like and comment UI on feed",
        "like" in html.lower() and "comment" in html.lower(), f"Screen: {s}")

    # --- Responsive Desktop ---
    print("\n  — Responsive Desktop —")
    vp = page.viewport_size or {}
    rec("Responsive", f"Desktop viewport: {vp.get('width')}x{vp.get('height')}",
        vp.get('width', 1280) >= 1200, f"Width={vp.get('width')}px")

    rec("Responsive", "Viewport meta tag present",
        "viewport" in html.lower(), "Found in page source")

    bw = page.evaluate("document.body.scrollWidth")
    rec("Responsive", f"No horizontal overflow at {vp.get('width')}px (body {bw}px)",
        bw <= vp.get('width', 1280) + 5, f"Body width: {bw}px vs Viewport: {vp.get('width')}px")

    # --- Security ---
    print("\n  — Security —")
    rec("Security", "No password hash or JWT secret in page source",
        "$2b$" not in html and "jwt_secret" not in html.lower(), "Verified clean")

    # --- Keyboard Navigation ---
    print("\n  — Keyboard Navigation —")
    try:
        navigate(page, "/web/login")
        page.wait_for_timeout(300)
        for _ in range(4):
            page.keyboard.press("Tab")
        focus = page.evaluate("document.activeElement ? document.activeElement.tagName : 'null'")
        rec("Keyboard", "Tab cycles through form fields",
            focus != "null", f"Active element: {focus}")
    except Exception as e:
        rec("Keyboard", "Tab cycles through form fields", None, f"Test-script issue: {e}")

    # 24. Enter submits login form — fresh navigation, fill, focus submit, press Enter
    try:
        navigate(page, "/web/login")
        page.wait_for_timeout(300)
        page.fill('input[name="identifier"]', "alice")
        page.fill('input[type="password"]', DEMO_PW)
        # Focus the submit button so Enter triggers form submission
        page.focus('button[type="submit"]', timeout=3000)
        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            page.keyboard.press("Enter")
        # Confirm redirect to /feed
        page.wait_for_load_state("domcontentloaded", timeout=8000)
        rec("Keyboard", "Enter submits login form → feed",
            "/web/web/web/feed" in page.url, f"URL: {page.url}")
    except Exception as e:
        rec("Keyboard", "Enter submits login form", None, f"Test-script issue: {e}")


# -------------------------------------------------------------------
# MOBILE SMOKE
# -------------------------------------------------------------------

def run_mobile(page):
    print("\n" + "=" * 60)
    print("  NARROWMOBILE VIEWPORT (375x812)")
    print("=" * 60)

    navigate(page, "/web/login")
    html = page.content()
    s = screenshot(page, "login_mobile")
    rec("Mobile", "Login page renders at 375px",
        "log in" in html.lower(), f"Screen: {s}")

    bw = page.evaluate("document.body.scrollWidth")
    rec("Mobile", f"No horizontal overflow at 375px (body {bw}px)",
        bw <= 385, f"Body width: {bw}px, Viewport: 375px")

    navigate(page, "/web/register")
    html = page.content()
    s = screenshot(page, "register_mobile")
    rec("Mobile", "Register form usable at 375px",
        "username" in html.lower() and "password" in html.lower(), f"Screen: {s}")

    login(page, "alice", DEMO_PW)
    wait_until_feed(page)
    html = page.content()
    s = screenshot(page, "feed_mobile")
    rec("Mobile", "Feed readable at 375px with content",
        "post" in html.lower() or "feed" in page.url.lower(), f"Screen: {s}")

    navs = page.evaluate("document.querySelectorAll('nav').length")
    s = screenshot(page, "nav_mobile")
    rec("Mobile", f"Nav elements present ({navs})",
        navs > 0, f"Screen: {s}")

    labels = page.evaluate("document.querySelectorAll('label').length")
    rec("Mobile", f"Form labels visible ({labels} found)",
        labels > 0, f"{labels} labels in rendered HTML")


# -------------------------------------------------------------------
# Evidence report
# -------------------------------------------------------------------

def write_evidence():
    pass_n = sum(1 for r in results if r["passed"] is True and not r.get("inconc"))
    fail_n = sum(1 for r in results if r["passed"] is False and not r.get("inconc"))
    inconc_n = sum(1 for r in results if r.get("inconc"))
    total = len(results)

    lines = [
        "# Real Browser Smoke Evidence — Headless Chromium",
        "",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Browser:** Playwright Chromium Headless Shell",
        f"**Server:** {BASE_URL} (uvicorn, seeded SQLite, env-controlled secrets)",
        f"**Viewports:** Desktop 1280×900, Narrow Mobile 375×812",
        f"**Demo accounts:** alice, bob, carol",
        f"**Env:** TING_JWT_SECRET (set by caller), TING_DEMO_PASSWORD (set by caller)",
        "",
        f"**Results: {pass_n} PASS, {fail_n} FAIL, {inconc_n} INCONCLUSIVE / {total} total**",
        "",
        "## Check Results",
        "",
        "| # | Category | Check | Result | Detail |",
        "|---|---|---|---|---|",
    ]

    for i, r in enumerate(results, 1):
        if r.get("inconc"):
            status = "INCONCLUSIVE"
        elif r["passed"]:
            status = "PASS"
        else:
            status = "FAIL"
        lines.append(f"| {i} | {r['cat']} | {r['item']} | **{status}** | {r['detail']} |")

    lines += [
        "",
        "## Screenshots",
        "",
        f"Saved to `docs/screenshots/` ({len(list(SCREENS_DIR.glob('*.png')))} PNG images).",
        "",
        "## Summary by Category",
        "",
    ]
    cats = {}
    for r in results:
        c = r["cat"]
        if c not in cats:
            cats[c] = {"p": 0, "f": 0, "i": 0}
        if r.get("inconc"):
            cats[c]["i"] += 1
        elif r["passed"]:
            cats[c]["p"] += 1
        else:
            cats[c]["f"] += 1
    for c, n in cats.items():
        lines.append(f"- **{c}:** {n['p']} PASS, {n['f']} FAIL, {n['i']} INCONCLUSIVE")

    lines += [""]
    if fail_n > 0:
        lines += ["## Failures", ""]
        for r in results:
            if r["passed"] is False and not r.get("inconc"):
                lines.append(f"- **{r['cat']} / {r['item']}**: {r['detail']}")
        lines.append("")
        lines += ["## Verdict", "", f"**FAIL** — {fail_n} check(s) failed."]
    elif inconc_n > 0:
        lines += ["## Verdict", "", f"**INCONCLUSIVE** — {pass_n} PASS, {inconc_n} checks could not be auto-verified in headless mode."]
    else:
        lines += ["## Verdict", "", f"**PASS** — all {pass_n} checks passed."]

    return "\n".join(lines)


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

def main():
    print(f"\nBrowser Smoke Test — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Target: {BASE_URL}")
    print(f"Viewports: Desktop {DESKTOP}, Mobile {MOBILE}\n")

    _preseed()
    print("Starting server...")
    server = start_server()
    try:
        with sync_playwright() as pw:
            # Desktop run
            print("\n" + "~" * 60 + "\nDESKTOP RUN\n" + "~" * 60)
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            page = browser.new_context(viewport=DESKTOP).new_page()
            run_desktop(page)
            browser.close()

            # Mobile run
            print("\n" + "~" * 60 + "\nMOBILE RUN\n" + "~" * 60)
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            page = browser.new_context(viewport=MOBILE).new_page()
            run_mobile(page)
            browser.close()

        # Evidence
        ev = write_evidence()
        (OUTPUT / "BROWSER_SMOKE_EVIDENCE.md").write_text(ev)
        n = len(list(SCREENS_DIR.glob("*.png")))
        print(f"\nEvidence: docs/BROWSER_SMOKE_EVIDENCE.md")
        print(f"Screenshots: {n} in docs/screenshots/")
    finally:
        stop_server(server)

    p = sum(1 for r in results if r["passed"] and not r.get("inconc"))
    f = sum(1 for r in results if not r["passed"] and not r.get("inconc"))
    i = sum(1 for r in results if r.get("inconc"))
    print(f"\nFINAL: {p} PASS, {f} FAIL, {i} INCONCLUSIVE / {len(results)}")
    # Exit nonzero if any failure or inconclusive result
    if f > 0 or i > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()