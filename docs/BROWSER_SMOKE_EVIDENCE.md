# Real Browser Smoke Evidence — Headless Chromium

**Date:** 2026-08-17 16:05:50
**Browser:** Playwright Chromium Headless Shell
**Server:** http://127.0.0.1:8765 (uvicorn, seeded SQLite, env-controlled secrets)
**Viewports:** Desktop 1280×900, Narrow Mobile 375×812
**Demo accounts:** alice, bob, carol
**Env:** TING_JWT_SECRET (set by caller), TING_DEMO_PASSWORD (set by caller)

**Results: 30 PASS, 0 FAIL, 0 INCONCLUSIVE / 30 total**

## Check Results

| # | Category | Check | Result | Detail |
|---|---|---|---|---|
| 1 | Auth | Register page renders with labels | **PASS** | Screen: docs/screenshots/register_desktop-160540.png |
| 2 | Auth | Register → redirects to feed | **PASS** | URL: http://127.0.0.1:8765/feed |
| 3 | Auth | Duplicate username shows error | **PASS** | Screen: docs/screenshots/dup_register-160542.png |
| 4 | Auth | Short username shows validation error | **PASS** | Screen: docs/screenshots/short_username-160543.png, URL: http://127.0.0.1:8765/register |
| 5 | Auth | Logout clears session (cookie gone → redirected) | **PASS** | URL: http://127.0.0.1:8765/login, Auth cookies: 0 |
| 6 | Auth | /feed denied after logout | **PASS** | URL: http://127.0.0.1:8765/feed |
| 7 | Auth | Wrong password → error, no secrets in HTML | **PASS** | Screen: docs/screenshots/wrong_pw-160544.png |
| 8 | Auth | Login as alice → feed | **PASS** | URL: http://127.0.0.1:8765/feed |
| 9 | Feed | Alice sees feed | **PASS** | URL: http://127.0.0.1:8765/feed, Screen: docs/screenshots/alice_feed-160544.png |
| 10 | Feed | ONLY_ME post created via form | **PASS** | Post visible in feed, Screen: docs/screenshots/only_me_post-160547.png |
| 11 | Feed | FRIENDS post created via form | **PASS** | Post visible in feed, Screen: docs/screenshots/friends_post-160547.png |
| 12 | Feed | Feed newest-first ordering | **PASS** | FRIENDS@2135 before ONLY_ME@4261 |
| 13 | Feed | Bob sees Alice's FRIENDS post in feed | **PASS** | Screen: docs/screenshots/bob_feed-160547.png |
| 14 | Feed | Carol's post NOT in Bob's feed | **PASS** | Carol visible: False |
| 15 | Profile | Bob's own profile renders | **PASS** | Screen: docs/screenshots/bob_profile-160547.png |
| 16 | Profile | View other user profile with relationship info | **PASS** | Screen: docs/screenshots/alice_profile_view-160547.png |
| 17 | Social | Relationship actions visible on profile | **PASS** | Screen: docs/screenshots/alice_profile_view-160547.png |
| 18 | Like/Comment | Like and comment UI on feed | **PASS** | Screen: docs/screenshots/interactions-160548.png |
| 19 | Responsive | Desktop viewport: 1280x900 | **PASS** | Width=1280px |
| 20 | Responsive | Viewport meta tag present | **PASS** | Found in page source |
| 21 | Responsive | No horizontal overflow at 1280px (body 1280px) | **PASS** | Body width: 1280px vs Viewport: 1280px |
| 22 | Security | No password hash or JWT secret in page source | **PASS** | Verified clean |
| 23 | Keyboard | Tab cycles through form fields | **PASS** | Active element: INPUT |
| 24 | Keyboard | Enter submits login form → feed | **PASS** | URL: http://127.0.0.1:8765/feed |
| 25 | Mobile | Login page renders at 375px | **PASS** | Screen: docs/screenshots/login_mobile-160549.png |
| 26 | Mobile | No horizontal overflow at 375px (body 375px) | **PASS** | Body width: 375px, Viewport: 375px |
| 27 | Mobile | Register form usable at 375px | **PASS** | Screen: docs/screenshots/register_mobile-160549.png |
| 28 | Mobile | Feed readable at 375px with content | **PASS** | Screen: docs/screenshots/feed_mobile-160550.png |
| 29 | Mobile | Nav elements present (1) | **PASS** | Screen: docs/screenshots/nav_mobile-160550.png |
| 30 | Mobile | Form labels visible (16 found) | **PASS** | 16 labels in rendered HTML |

## Screenshots

Saved to `docs/screenshots/` (158 PNG images).

## Summary by Category

- **Auth:** 8 PASS, 0 FAIL, 0 INCONCLUSIVE
- **Feed:** 6 PASS, 0 FAIL, 0 INCONCLUSIVE
- **Profile:** 2 PASS, 0 FAIL, 0 INCONCLUSIVE
- **Social:** 1 PASS, 0 FAIL, 0 INCONCLUSIVE
- **Like/Comment:** 1 PASS, 0 FAIL, 0 INCONCLUSIVE
- **Responsive:** 3 PASS, 0 FAIL, 0 INCONCLUSIVE
- **Security:** 1 PASS, 0 FAIL, 0 INCONCLUSIVE
- **Keyboard:** 2 PASS, 0 FAIL, 0 INCONCLUSIVE
- **Mobile:** 6 PASS, 0 FAIL, 0 INCONCLUSIVE

## Verdict

**PASS** — all 30 checks passed.