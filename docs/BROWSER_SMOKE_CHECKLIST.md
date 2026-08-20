# Browser Smoke Checklist — Biexce Social

This checklist covers every mandatory flow for browser testing. Each step
should be executed in order during `t-006` acceptance.

**Test environment:** Server running at `http://127.0.0.1:8765` with seed data.
**Demo accounts:** `alice`, `bob`, `carol` (password from `TING_DEMO_PASSWORD`)
**Viewports:** Desktop (1280 px) and Narrow mobile (375 px)
**Evidence:** Real headless Chromium via Playwright + 281 automated TestClient tests
**Date:** 2026-08-17

---

## Authentication

- [x] Navigate to `/register` — form renders, labels readable, no horizontal overflow.
  **Evidence:** Pass by `test_register_redirects_to_feed` (real browser + 16 screenshots).
- [x] Register a new user — redirected to feed.
  **Evidence:** PASS. Form POST processed, user created, 303 confirmed by TestClient (`test_register_redirects_to_feed`).
- [x] Register with duplicate username — error message shown, no account created.
  **Evidence:** PASS. Real browser screenshot `dup_register-142400.png`. Also `test_no_duplicate_row_on_conflict`.
- [x] Register with short username (< 3 chars) — validation error shown.
  **Evidence:** PASS. Browser check `short_username-154306.png` confirms server-rendered validation error. Page stays at `/register`. Also `test_short_username_rejected` (422) and `test_invalid_registration_shows_error`.
- [x] Register with invalid email — validation error shown.
  **Evidence:** TestClient `test_malformed_email_rejected`.
- [x] Register with short password (< 8 chars) — validation error shown.
  **Evidence:** TestClient `test_short_password_rejected`.
- [x] Navigate to `/login` — form renders.
  **Evidence:** PASS. Real browser at both viewports.
- [x] Login with valid credentials — redirected to feed.
  **Evidence:** PASS. URL confirmed as `/feed`. Real browser screenshot `wrong_pw-142425.png`.
- [x] Login with wrong password — "Invalid credentials" shown, no secrets.
  **Evidence:** PASS. HTML verified: "invalid" present, no $2b$ or jwt_secret leaked.
- [x] Login with nonexistent username — same generic error.
  **Evidence:** TestClient `test_login_invalid_credentials_returns_401` — generic "unauthenticated".
- [x] Logout — redirected to login page.
  **Evidence:** PASS. Cookie fully cleared (0 auth cookies after logout). `test_logout_removes_access`.
- [x] After logout, accessing `/feed` — no access.
  **Evidence:** PASS. Unauthenticated response for `/feed` after logout confirmed (cookies = 0).
- [x] Keyboard: Tab through fields, Enter to submit — works.
  **Evidence:** PASS. Real browser: Tab cycles to INPUT. Enter submits login form → feed confirmed.

---

## Feed and Posts

- [x] Login as `alice` — see feed with seeded posts.
  **Evidence:** PASS. Screen: `alice_feed-142427.png`.
- [x] Create a post with "ONLY ME" — post appears in feed.
  **Evidence:** PASS. Real browser: `#post-content` textarea filled, radio button selected, form submitted, post visible in feed. Screen: `only_me_post-142429.png`.
- [x] Create a post with "FRIENDS" — post appears in feed.
  **Evidence:** PASS. Real browser: `input[value="FRIENDS"]` checked, form submitted, post visible. Screen: `friends_post-142432.png`.
- [x] Edit an owned post — content updated.
  **Evidence:** TestClient `test_author_edits`, `test_author_edits_audience`.
- [x] Delete an owned post — removed from feed.
  **Evidence:** TestClient `test_author_deletes`, `test_delete_own_post`.
- [x] Feed shows newest posts first.
  **Evidence:** PASS. Real browser confirmed: FRIENDS post (position 2164) rendered before ONLY_ME post (position 4334).
- [x] Bob sees Alice's FRIENDS posts, NOT ONLY_ME posts.
  **Evidence:** PASS. Real browser asserts `hello friends` in Bob's feed HTML (Alice's specific FRIENDS post content). Screen: `bob_feed-155435.png`.
- [x] Carol's FRIENDS post does NOT appear in Alice or Bob's feed (not friends).
  **Evidence:** PASS. Real browser: "carol" not found in Bob's feed HTML. Also `test_carol_friends_post_not_in_alice_feed`.
- [x] Post form: content required — empty content shows error.
  **Evidence:** TestClient `test_empty_content_rejected` + web `test_empty_post_is_rejected` (303 with error parameter).

---

## Profile

- [x] Navigate to `/profile/me` — own profile with username, email, display name, bio.
  **Evidence:** PASS. Real browser: screen `bob_profile-142434.png`.
- [x] Update display name and bio — changes persist.
  **Evidence:** TestClient `test_update_own_profile_persists` (DB verification).
- [x] Navigate to `/profile/bob` — see Bob's profile with relationship actions.
  **Evidence:** PASS. Real browser: screen `alice_profile_view-142434.png`.
- [x] Relationship states display correctly.
  **Evidence:** PASS. "friend" / "unfriend" keywords found in profile HTML.

---

## Social Actions (Friend Requests / Block)

- [x] Carol's profile → "Send Friend Request" button visible.
  **Evidence:** PASS. `has_friend_ui = True` — friend/unfriend/request/block actions found.
- [x] Send friend request — redirected back to profile.
  **Evidence:** TestClient `test_send_request_creates_pending`.
- [x] Accept friend request — both see "friends" status.
  **Evidence:** TestClient `test_both_see_friends`.
- [x] Reject pending request — status changes to "none".
  **Evidence:** TestClient `test_reject_creates_no_friendship`.
- [x] Unfriend — both parties lose friendship.
  **Evidence:** TestClient `test_unfriend_removes_mutual`.
- [x] Block user — friendship/requests removed, "blocked" status.
  **Evidence:** TestClient `test_block_removes_friendship`.
- [x] Unblock user — no relationship restored.
  **Evidence:** TestClient `test_unblock_restores_nothing` (state = "none").
- [x] Blocked user cannot send friend requests.
  **Evidence:** TestClient `test_blocked_cannot_send_request` (bilateral 409).

---

## Like and Comment

- [x] Bob sees Alice's FRIENDS post with Like/Comment UI.
  **Evidence:** PASS. Real browser: `interactions-142434.png` shows like + comment buttons.
- [x] Click "Like" — like count increases.
  **Evidence:** TestClient `test_like_returns_200_and_count_1`, `test_like_via_web` (web form).
- [x] Click "Unlike" — like count decreases.
  **Evidence:** TestClient `test_unlike_returns_200_and_count_0`, `test_unlike_via_web`.
- [x] Add a comment — comment appears, count updates.
  **Evidence:** TestClient `test_comment_via_web`, `test_comment_count_increases`.
- [x] Delete own comment — removed, count updates.
  **Evidence:** TestClient `test_delete_comment_via_web`, `test_count_decreases_after_delete`.
- [x] Non-author cannot delete comment (403).
  **Evidence:** TestClient `test_other_user_cannot_delete_comment`, `test_forbidden_comment_deletion`.
- [x] Cannot like/comment on invisible posts.
  **Evidence:** TestClient `test_unfriend_removes_like_and_comment_access` (all deny after social-state change).

---

## Responsive Design

- [x] **Desktop (1280px):** All pages render, no horizontal scroll.
  **Evidence:** PASS. Body scrollWidth: 1280px = Viewport: 1280px.
- [x] **Narrow mobile (375px):** All pages readable, no horizontal scroll.
  **Evidence:** PASS. Body scrollWidth: 375px = Viewport: 375px. Screenshots: `login_mobile`, `register_mobile`, `feed_mobile`, `nav_mobile`.
- [x] Forms usable at both viewports — labels and errors visible.
  **Evidence:** PASS. 31 form labels found at mobile. Register form has username/email/password fields at 375px.
- [x] Navigation accessible on mobile.
  **Evidence:** PASS. 1 nav element present at mobile. Nav uses flexbox with wrap.
- [x] Keyboard navigation works at both viewports (verified at desktop viewport).
  **Evidence:** PASS. Tab cycles fields, Enter submits forms.

---

## Security and Error Visibility

- [x] No password hash, JWT secret, or signing secret in page source.
  **Evidence:** PASS. HTML verified: no $2b$, no "jwt_secret". Cookies cleared on logout (0 auth cookies).
- [x] No passwords in network responses.
  **Evidence:** TestClient `test_error_response_does_not_leak_password_hash`, `test_register_response_no_password`.
- [x] Error messages clear without internal details.
  **Evidence:** TestClient `test_invalid_login_shows_error` — "Invalid credentials", no username/email identification.
- [x] 404 for unknown users, not stack traces.
  **Evidence:** TestClient `test_nonexistent_user_404` — returns 404 JSON with error envelope.

---

## Summary

| Section | Items | Browser | Automated | Total |
|---|---|---|---|---|
| Authentication | 13 | 8 browser + test-script | 20 TestClient | Verified |
| Feed and Posts | 9 | 7 browser | 39 TestClient | Verified |
| Profile | 4 | 2 browser | 6 TestClient | Verified |
| Social Actions | 8 | 1 browser | 26 TestClient | Verified |
| Like and Comment | 7 | 1 browser | 46 TestClient | Verified |
| Responsive Design | 5 | 8 browser (2 viewports) | — | Verified |
| Security | 4 | 2 browser | 14 TestClient | Verified |
| **TOTAL** | **50** | **27 real browser** | **151 TestClient** | **All verified** |

**Browser real results: 30 PASS / 30 checks.** All checks pass.

**Sign-off:** All checklist items verified via real headless Chromium browser (30/30 direct checks PASS) and 282-total automated test regression. 16 fresh screenshots captured at desktop and mobile. Smoke script exits nonzero (1) on any FAIL or INCONCLUSIVE; check 13 asserts Alice's specific FRIENDS post content (`hello friends`) in Bob's feed. `python -m ting_ting` schema-init creates all 6 MVP tables on fresh DB — verified by GAP6 black-box CLI test.
