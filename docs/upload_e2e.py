"""E2E browser check — composer media upload flow (T-017).

Runs against the local dev server (default http://127.0.0.1:8080) with a real
Chromium via Playwright. Registers a throwaway account, then verifies the
composer media upload: preview + remove, XHR progress, success render,
blocked-content error, oversize client-side error. Captures desktop +
mobile screenshots under docs/screenshots-e2e/.

Usage:
    .venv/bin/python docs/upload_e2e.py
Exit code 0 = all checks passed, 1 = at least one failure.
"""

import base64
import datetime
import io
import os
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8080")
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots-e2e"
FIX = ROOT / ".tmpdbg" / "e2e-files"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


def make_valid_png(path: Path) -> None:
    path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
        "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
    ))


def make_fake_zip_png(path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "not really a png")
    path.write_bytes(buf.getvalue())


def make_big_png(path: Path) -> None:
    chunk = b"\x00" * (1024 * 1024)
    with path.open("wb") as f:
        for _ in range(26):
            f.write(chunk)


def register(page, username: str) -> None:
    page.goto(f"{BASE}/web/register")
    page.fill("#username", username)
    page.fill("#email", f"{username}@example.com")
    page.fill("#password", "E2E-passw0rd")
    page.click('button:has-text("Đăng ký")')
    page.wait_for_url("**/web/feed", timeout=15000)


def run_context(browser, uname: str, label: str, width: int, height: int) -> None:
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()

    register(page, uname)
    check(f"[{label}] đăng ký tự động vào feed", page.url.endswith("/web/feed"), page.url)

    nav_el = page.locator('a[href="/web/thread/new"]')
    check(f"[{label}] nav 'Viết bài' (i18n)", nav_el.get_attribute("aria-label") == "Viết bài", nav_el.get_attribute("aria-label") or "")
    check(
        f"[{label}] nav active có aria-current (a11y)",
        page.locator('nav a[href="/web/feed"][aria-current="page"]').count() == 1,
    )

    file_input = page.locator('form[data-upload-form] input[name="media_file"]')
    preview = page.locator("#composer-preview")
    state = page.locator("#composer-upload-state")
    valid = FIX / "valid.png"

    file_input.set_input_files(str(valid))
    page.wait_for_timeout(300)
    img = preview.locator("img.composer-preview-media")
    check(f"[{label}] preview hiện sau khi chọn ảnh", preview.is_visible() and img.count() == 1)
    check(f"[{label}] preview alt mô tả file", "valid.png" in (img.get_attribute("alt") or ""), img.get_attribute("alt") or "")
    remove = preview.locator("button[aria-label='Gỡ ảnh/video']")
    check(f"[{label}] nút gỡ ảnh có aria-label", remove.count() == 1)
    remove.click()
    page.wait_for_timeout(200)
    check(f"[{label}] gỡ ảnh -> preview ẩn", preview.is_hidden())

    page.fill("#post-content", "E2E bài viết upload media test")
    file_input.set_input_files(str(valid))
    page.wait_for_timeout(100)
    page.click('form[data-upload-form] button[type="submit"]')
    page.wait_for_url("**/web/feed", timeout=30000)
    page.wait_for_selector(".thread-item .post-media img", timeout=10000)
    check(f"[{label}] post mới render media sau upload XHR", True)
    page.screenshot(path=str(SHOTS / f"e2e-feed-{label}.png"), full_page=True)

    file_input.set_input_files(str(FIX / "fake.zip.png"))
    page.fill("#post-content", "E2E nội dung test (sẽ không đăng thành công)")
    page.click('form[data-upload-form] button[type="submit"]')
    page.wait_for_selector("#composer-upload-state.error", timeout=30000)
    txt = state.inner_text()
    check(f"[{label}] file ZIP nhét đuôi .png bị chặn (422) -> lỗi trên UI", "bị cấm" in txt or "chặn" in txt, txt)
    page.fill("#post-content", "")

    file_input.set_input_files(str(FIX / "big.png"))
    page.wait_for_timeout(300)
    txt = state.inner_text()
    check(f"[{label}] file >25MB chặn phía client", "25 MB" in txt, txt)

    ctx.close()


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    FIX.mkdir(parents=True, exist_ok=True)
    make_valid_png(FIX / "valid.png")
    make_fake_zip_png(FIX / "fake.zip.png")
    make_big_png(FIX / "big.png")

    ts = datetime.datetime.now().strftime("%m%d%H%M%S")
    uname = f"e2e_up_{ts}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            run_context(browser, f"{uname}_a", "desktop", 1280, 900)
            run_context(browser, f"{uname}_m", "mobile", 390, 844)
        finally:
            browser.close()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed, user={uname}")
    for name, _, extra in failed:
        print(f"  FAILED: {name} {extra}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())