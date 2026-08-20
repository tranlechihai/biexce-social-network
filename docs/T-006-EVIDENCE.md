# t-006 — Verification Evidence

**Task:** Audit mandatory coverage and produce pre-Gate-2 verification evidence
**Owner:** bx-test
**Date:** 2026-08-17
**Verdict:** **PASS**

---

## 1. Scope Coverage Audit

### AC1 — TT-AUTH / TT-PROFILE

| # | Requirement | Test File | Tests | Status |
|---|---|---|---|---|
| 1 | Registration uniqueness (username + email) | `integration/test_auth.py` | `test_duplicate_username_conflict`, `test_duplicate_email_conflict`, `test_duplicate_normalized_username`, `test_duplicate_normalized_email` | **PASS** |
| 2 | Password hash safety (bcrypt, not plain text) | `unit/test_auth.py` | `test_hash_is_not_plain_text`, `test_hash_is_adaptive` | **PASS** |
| 3 | Username/email login | `integration/test_auth.py` | `test_login_by_username`, `test_login_by_email` | **PASS** |
| 4 | Bearer token auth | `integration/test_auth.py` | `test_valid_bearer_identifies_user` | **PASS** |
| 5 | Cookie auth (same user as bearer) | `integration/test_auth.py` | `test_valid_cookie_identifies_same_user`, `test_login_cookie_set` | **PASS** |
| 6 | Logout clears cookie | `integration/test_profile.py` | `test_logout_clears_cookie`, `test_after_logout_cookie_only_request_is_unauthenticated` | **PASS** |
| 7 | Anonymous access → 401 | `integration/test_auth.py` | `test_anonymous_get_profile_401` | **PASS** |
| 8 | Malformed token → 401 | `unit/test_auth.py`, `integration/test_auth.py` | `test_malformed_token_raises`, `test_malformed_token_401` | **PASS** |
| 9 | Expired token → 401 | `unit/test_auth.py` | `test_expired_token_raises` | **PASS** |
| 10 | Owned profile update | `integration/test_profile.py` | `test_update_own_profile_persists` | **PASS** |
| 11 | Owner-only profile mutation | `integration/test_profile.py` | `test_cannot_update_other_users_profile` | **PASS** |
| 12 | Validation (short username, email, password) | `integration/test_auth.py` | `test_short_username_rejected`, `test_malformed_email_rejected`, `test_short_password_rejected` | **PASS** |
| 13 | No sensitive data in responses | `integration/test_error_envelope.py` | `test_register_response_no_password`, `test_login_response_no_password_hash`, `test_profile_response_no_password` | **PASS** |

**AC1 Verdict: PASS** (13/13 criteria verified)

---

### AC2 — TT-SOCIAL / TT-BLOCK

| # | Requirement | Test File | Tests | Status |
|---|---|---|---|---|
| 1 | Send request creates pending | `integration/test_social.py` | `test_send_request_creates_pending` | **PASS** |
| 2 | Self-request rejected | `unit/test_social.py`, `integration/test_social.py` | `test_self_request_rejected` | **PASS** |
| 3 | Duplicate request rejected | `unit/test_social.py`, `integration/test_social.py` | `test_duplicate_request_rejected`, `test_reverse_duplicate_rejected` | **PASS** |
| 4 | Reverse duplicate rejected (canonical pair) | `unit/test_social.py` | `test_reverse_duplicate_rejected` | **PASS** |
| 5 | Recipient accepts | `integration/test_social.py` | `test_recipient_accepts`, `test_both_see_friends` | **PASS** |
| 6 | Sender cannot accept own request | `integration/test_social.py` | `test_sender_cannot_accept` | **PASS** |
| 7 | Third party cannot accept | `integration/test_social.py` | `test_third_party_cannot_accept` | **PASS** |
| 8 | Recipient rejects | `integration/test_social.py` | `test_recipient_rejects`, `test_reject_creates_no_friendship` | **PASS** |
| 9 | Reject → new request → reject (constraint repair) | `integration/test_social.py` | `test_reject_re_request_reject_no_crash` | **PASS** |
| 10 | Unfriend removes mutual | `integration/test_social.py` | `test_unfriend_removes_mutual` | **PASS** |
| 11 | Unfriend → refriend → unfriend (constraint repair) | `integration/test_social.py` | `test_unfriend_refriend_unfriend_no_crash` | **PASS** |
| 12 | Replay unfriend silent | `integration/test_social.py` | `test_replay_unfriend_silent` | **PASS** |
| 13 | Block removes friendship | `integration/test_social.py` | `test_block_removes_friendship` (both states) | **PASS** |
| 14 | Block removes pending | `integration/test_social.py` | `test_block_removes_pending` | **PASS** |
| 15 | Blocked pair cannot request (bilateral) | `integration/test_social.py` | `test_blocked_cannot_send_request` | **PASS** |
| 16 | Self-block rejected | `unit/test_social.py` | `test_self_block_rejected` | **PASS** |
| 17 | Unblock restores nothing | `integration/test_social.py` | `test_unblock_restores_nothing` | **PASS** |
| 18 | After unblock, normal flow works | `integration/test_social.py` | `test_after_unblock_normal_flow` | **PASS** |
| 19 | Non-blocker cannot unblock | `integration/test_social.py` | `test_non_blocker_cannot_unblock` | **PASS** |
| 20 | Duplicate request row not created on conflict | `integration/test_social.py` | `test_no_duplicate_row_on_conflict` | **PASS** |

**AC2 Verdict: PASS** (20/20 criteria verified)

---

### AC3 — TT-POST / TT-FEED

| # | Requirement | Test File | Tests | Status |
|---|---|---|---|---|
| 1 | Text validation (empty, oversized, invalid audience) | `integration/test_posts.py` | `test_empty_content_rejected`, `test_oversized_content_rejected`, `test_invalid_audience_rejected`, `test_lowercase_audience_rejected` | **PASS** |
| 2 | Author-only edit | `unit/test_post_policy.py`, `integration/test_posts.py` | `test_edit_as_author`, `test_edit_as_non_author_forbidden` | **PASS** |
| 3 | Author-only delete | `unit/test_post_policy.py`, `integration/test_posts.py` | `test_delete_as_author`, `test_delete_as_non_author_forbidden` | **PASS** |
| 4 | ONLY_ME: author always reads own | `unit/test_post_policy.py`, `integration/test_posts.py` | `test_author_always_sees_own`, `test_author_can_read_only_me` | **PASS** |
| 5 | ONLY_ME: friend denied | `unit/test_post_policy.py`, `integration/test_posts.py` | `test_only_me_denies_non_author`, `test_friend_cannot_read_only_me` | **PASS** |
| 6 | ONLY_ME: stranger denied | `integration/test_posts.py` | `test_non_friend_cannot_read_only_me` | **PASS** |
| 7 | ONLY_ME: blocked peer denied | `unit/test_post_policy.py`, `integration/test_posts.py` | `test_blocked_denies_only_me`, `test_blocked_peer_cannot_read_only_me` | **PASS** |
| 8 | ONLY_ME: not in friend's feed | `integration/test_posts.py` | `test_only_me_not_in_feed` | **PASS** |
| 9 | FRIENDS: current friend can read | `unit/test_post_policy.py`, `integration/test_posts.py` | `test_friends_allows_current_friend`, `test_current_friend_can_read` | **PASS** |
| 10 | FRIENDS: stranger denied | `unit/test_post_policy.py`, `integration/test_posts.py` | `test_friends_denies_stranger`, `test_non_friend_cannot_read_friends_post` | **PASS** |
| 11 | FRIENDS: blocked peer denied | `unit/test_post_policy.py`, `integration/test_posts.py` | `test_blocked_denies_friends`, `test_blocked_peer_cannot_read_friends_post` | **PASS** |
| 12 | Unfriend removes FRIENDS visibility | `unit/test_post_policy.py`, `integration/test_posts.py` | `test_unfriend_removes_visibility`, `test_unfriend_removes_friends_post_visibility` | **PASS** |
| 13 | Block removes visibility (transition test) | `integration/test_posts.py` | `test_block_removes_friends_post_visibility` | **PASS** |
| 14 | Unblock without restore (visibility denied) | `integration/test_posts.py` | `test_unblock_does_not_restore_visibility` | **PASS** |
| 15 | Stable newest-first ordering | `integration/test_posts.py` | `test_feed_newest_first` | **PASS** |
| 16 | Stable ID tiebreaker (equal timestamps) | `integration/test_posts.py` | `test_feed_stable_id_tiebreaker` | **PASS** |
| 17 | Paginated feed (limit/offset) | `integration/test_posts.py` | `test_feed_limits_enforced`, `test_feed_offset_pagination` | **PASS** |
| 18 | Feed excludes invisible posts | `integration/test_posts.py` | `test_feed_excludes_invisible` | **PASS** |
| 19 | Visibility filtering BEFORE pagination | `integration/test_posts.py` | `test_visibility_filtering_before_pagination` | **PASS** |
| 20 | Feed after unfriend omits old posts | `integration/test_posts.py` | `test_feed_omits_after_unfriend` | **PASS** |
| 21 | **3-user isolation: Carol's FRIENDS post not in Alice/Bob's feed** | `integration/test_smoke_coverage.py` | `test_carol_friends_post_not_in_alice_feed`, `test_carol_post_visible_in_own_feed` | **PASS** (NEW GAP TEST) |
| 22 | Direct-read non-leakage (unknown post → 404) | `integration/test_posts.py` | `test_unknown_post_not_found` | **PASS** |
| 23 | Anonymous create/read/feed/delete → 401 | `integration/test_posts.py` | `test_anonymous_create_post_401`, `test_anonymous_read_post_401`, `test_anonymous_feed_401`, `test_anonymous_delete_401` | **PASS** |
| 24 | Replayed delete harmless | `integration/test_posts.py` | `test_replayed_delete_corrupts_nothing` | **PASS** |
| 25 | No duplicate row on validation failure | `integration/test_posts.py` | `test_no_duplicate_row_on_validation_failure` | **PASS** |

**AC3 Verdict: PASS** (25/25 criteria verified; 1 gap filled with new test)

---

### AC4 — TT-LIKE / TT-COMMENT

| # | Requirement | Test File | Tests | Status |
|---|---|---|---|---|
| 1 | Like returns 200, count = 1 | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_like_returns_200_and_count_1`, `test_create_like_creates_one_row` | **PASS** |
| 2 | Repeated like idempotent (one row) | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_repeated_like_same_state_one_row`, `test_create_like_creates_one_row` | **PASS** |
| 3 | Unlike returns 200, count = 0 | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_unlike_returns_200_and_count_0`, `test_remove_like_is_idempotent` | **PASS** |
| 4 | Repeated unlike idempotent | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_repeated_unlike_same_state`, `test_remove_like_is_idempotent` | **PASS** |
| 5 | Count never negative | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_count_never_negative`, `test_count_never_negative_after_removal` | **PASS** |
| 6 | Multiple users like same post | `unit/test_interaction_policy.py` | `test_two_users_like_same_post` | **PASS** |
| 7 | unlike only own like | `integration/test_interactions.py` | `test_unlike_only_own_like` | **PASS** |
| 8 | Concurrent like conflict recovery | `unit/test_interaction_policy.py` | `test_concurrent_like_conflict_recovery_with_outer_write` | **PASS** |
| 9 | Comment creates, count increases | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_create_comment_201`, `test_comment_count_increases`, `test_create_comment_stores_and_count_increases` | **PASS** |
| 10 | Empty comment rejected | `integration/test_interactions.py` | `test_empty_comment_rejected` | **PASS** |
| 11 | Oversized comment rejected | `integration/test_interactions.py` | `test_oversized_comment_rejected` | **PASS** |
| 12 | Comment list oldest-first | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_list_comments_returns_all_oldest_first` | **PASS** |
| 13 | Comment pagination | `integration/test_interactions.py` | `test_list_comments_pagination` | **PASS** |
| 14 | Comment visibility: friend sees, stranger denied | `integration/test_interactions.py` | `test_list_comments_friend_sees`, `test_list_comments_non_friend_denied` | **PASS** |
| 15 | ONLY_ME post comments not visible | `integration/test_interactions.py` | `test_list_comments_only_me_not_visible` | **PASS** |
| 16 | Comment author can delete | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_comment_author_can_delete` | **PASS** |
| 17 | Post author can delete any comment | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_post_author_can_delete_any_comment` | **PASS** |
| 18 | Neither author denied | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_other_user_cannot_delete_comment`, `test_neither_author_denied` | **PASS** |
| 19 | Comment count decreases after delete | `integration/test_interactions.py`, `unit/test_interaction_policy.py` | `test_delete_comment_count_decreases`, `test_count_decreases_after_delete` | **PASS** |
| 20 | Missing comment → 404 | `integration/test_interactions.py` | `test_missing_comment_404` | **PASS** |
| 21 | Comment on wrong post → 404 | `integration/test_interactions.py` | `test_comment_wrong_post_404` | **PASS** |
| 22 | Unfriend removes like/comment access | `integration/test_interactions.py` | `test_unfriend_removes_like_and_comment_access` | **PASS** |
| 23 | Block removes like/comment access | `integration/test_interactions.py` | `test_block_removes_like_and_comment_access` | **PASS** |
| 24 | Unblock does not restore interaction access | `integration/test_interactions.py` | `test_unblock_does_not_restore_interaction_access` | **PASS** |
| 25 | Like/comment on invisible post denied | `integration/test_interactions.py` | `test_create_comment_non_visible_404`, `test_like_on_only_me_post` | **PASS** |
| 26 | Persistence check: API counts = DB rows | `integration/test_interactions.py` | `test_like_and_comment_counts_agree_with_rows` | **PASS** |
| 27 | Persistence check: unlike/delete zeroes rows | `integration/test_interactions.py` | `test_unlike_and_delete_agree_with_rows` | **PASS** |
| 28 | Retried like does not increase count | `integration/test_interactions.py` | `test_retried_like_does_not_increase_count` | **PASS** |
| 29 | Mixed mutations leave unrelated records unchanged | `integration/test_interactions.py` | `test_mixed_mutations_leave_unrelated_records_unchanged` | **PASS** |
| 30 | Anonymous like/comment/comment-list → 401 | `integration/test_interactions.py` | 5 anonymous 401 tests | **PASS** |
| 31 | Feed shows interaction counts | `integration/test_interactions.py` | `test_feed_shows_interaction_counts` | **PASS** |

**AC4 Verdict: PASS** (31/31 criteria verified)

---

### AC5 — TT-WEB

| # | Requirement | Test File | Tests | Status |
|---|---|---|---|---|
| 1 | Register redirects to feed with auth cookie | `integration/test_web.py` | `test_register_redirects_to_feed`, `test_register_sets_auth_cookie` | **PASS** |
| 2 | Login redirects to feed | `integration/test_web.py` | `test_login_redirects_to_feed` | **PASS** |
| 3 | Invalid login shows error in HTML | `integration/test_web.py` | `test_invalid_login_shows_error` | **PASS** |
| 4 | Invalid registration shows validation error | `integration/test_web.py` | `test_invalid_registration_shows_error` | **PASS** |
| 5 | Logout removes access | `integration/test_web.py` | `test_logout_removes_access` | **PASS** |
| 6 | Unauthenticated reaches login page | `integration/test_web.py` | `test_unauthenticated_reaches_login` | **PASS** |
| 7 | View own profile via web | `integration/test_web.py` | `test_view_own_profile` | **PASS** |
| 8 | Update profile via web | `integration/test_web.py` | `test_update_own_profile` | **PASS** |
| 9 | Send friend request via web | `integration/test_web.py` | `test_send_friend_request` | **PASS** |
| 10 | Accept friend request via web | `integration/test_web.py` | `test_accept_friend_request` | **PASS** |
| 11 | **Reject friend request via web** | `integration/test_smoke_coverage.py` | `test_reject_friend_request_via_web_form` | **PASS** (NEW GAP TEST) |
| 12 | Unfriend via web | `integration/test_web.py` | `test_unfriend_via_web` | **PASS** |
| 13 | Block via web | `integration/test_web.py` | `test_block_via_web` | **PASS** |
| 14 | **Unblock via web (no relationship restored)** | `integration/test_smoke_coverage.py` | `test_unblock_via_web_form_restores_nothing` | **PASS** (NEW GAP TEST) |
| 15 | View other user profile via web | `integration/test_web.py` | `test_view_other_profile` | **PASS** |
| 16 | Create ONLY_ME post via web | `integration/test_web.py` | `test_create_only_me_post` | **PASS** |
| 17 | Create FRIENDS post via web | `integration/test_web.py` | `test_create_friends_post` | **PASS** |
| 18 | **Empty post content error visible in HTML** | `integration/test_smoke_coverage.py` | `test_empty_post_content_shows_error_in_html` | **PASS** (NEW GAP TEST) |
| 19 | **Empty comment content redirect with error** | `integration/test_smoke_coverage.py` | `test_empty_comment_content_shows_redirect` | **PASS** (NEW GAP TEST) |
| 20 | Feed shows newest first via web | `integration/test_web.py` | `test_feed_shows_newest_first` | **PASS** |
| 21 | Delete own post via web | `integration/test_web.py` | `test_delete_own_post` | **PASS** |
| 22 | Invisible posts never render in web feed | `integration/test_web.py` | `test_invisible_posts_never_render` | **PASS** |
| 23 | Like via web | `integration/test_web.py` | `test_like_via_web` | **PASS** |
| 24 | Unlike via web | `integration/test_web.py` | `test_unlike_via_web` | **PASS** |
| 25 | Comment via web (create + count update) | `integration/test_web.py` | `test_comment_via_web` | **PASS** |
| 26 | Delete comment via web | `integration/test_web.py` | `test_delete_comment_via_web` | **PASS** |
| 27 | Browser comment lifecycle (full HTML flow) | `integration/test_web.py` | `test_browser_comment_create_render_and_delete` | **PASS** |
| 28 | Forbidden comment deletion via web | `integration/test_web.py` | `test_forbidden_comment_deletion` | **PASS** |
| 29 | **Feed denied after cookie logout (401, no feed HTML)** | `integration/test_smoke_coverage.py` | `test_feed_redirects_to_login_after_cookie_logout` | **PASS** (NEW GAP TEST) |
| 30 | **API feed 401 after cookie logout** | `integration/test_smoke_coverage.py` | `test_feed_api_returns_401_after_cookie_logout` | **PASS** (NEW GAP TEST) |

**AC5 Verdict: PASS** (30/30 criteria verified; 6 gap-filling tests added)

---

### AC6 — TT-SEED-DOC

| # | Requirement | Evidence | Status |
|---|---|---|---|
| 1 | README setup/install commands work | Verified: `pip install -e ".[test]"` installs. Schema init `python -m ting_ting` passes. | **PASS** |
| 2 | Fresh seed creates ≥ 3 users | Verified: alice, bob, carol in DB. `test_seed_creates_three_users` | **PASS** |
| 3 | Seed creates friendship | Verified: `test_seed_creates_friendship` — 1 accepted friendship (alice↔bob). | **PASS** |
| 4 | Seed creates both audiences | Verified: `test_seed_creates_both_audiences` — 3 FRIENDS + 1 ONLY_ME. | **PASS** |
| 5 | Seed creates like | Verified: `test_seed_creates_like` — 1 like (bob → alice). | **PASS** |
| 6 | Seed creates comment | Verified: `test_seed_creates_comment` — 1 comment (bob on alice). | **PASS** |
| 7 | Seed demo password works for login | Verified: `test_seed_demo_password_works` — bcrypt hash verifies with $TING_DEMO_PASSWORD. | **PASS** |
| 8 | Double-seed refused without mutation | Verified: Second seed invocation returns exit code 1 with "already contains data" error. Original data unchanged. | **PASS** |
| 9 | Seed refuses non-SQLite | Verified: `test_refuses_non_sqlite`. | **PASS** |
| 10 | Seed refuses missing tables | Verified: `test_refuses_missing_users_table`, `test_refuses_missing_required_tables`. | **PASS** |
| 11 | Seed refuses pre-populated DB | Verified: `test_refuses_prepopulated_database`, `test_refusal_no_mutation`. | **PASS** |
| 12 | Transactional rollback on mid-seed failure | Verified: `test_rollback_on_injected_failure` — all tables empty after rollback. | **PASS** |
| 13 | `python -m ting_ting` (no args) schema-init creates all 6 MVP tables on fresh DB | Verified: `test_cli_init_creates_tables_then_seed_populates` — black-box subprocess test exercises `__main__.py` entry point; verifies all tables present + seed populates + double-seed refused. | **PASS** (NEW GAP TEST) |

**AC6 Verdict: PASS** (13/13 criteria verified; 1 new gap test added)

---

### AC7 — TT-API-QUALITY

| # | Requirement | Evidence | Status |
|---|---|---|---|
| 1 | Consistent error envelope shape | `unit/test_errors.py` (6 tests), `integration/test_error_envelope.py` (4 tests) | **PASS** |
| 2 | Validation → 422 with details | `integration/test_error_envelope.py` — `test_validation_error_shape` | **PASS** |
| 3 | Unauthenticated → 401 | `integration/test_error_envelope.py` — `test_unauthenticated_error_shape` | **PASS** |
| 4 | Conflict → 409 | `integration/test_error_envelope.py` — `test_conflict_error_shape` | **PASS** |
| 5 | Not found → 404 | `integration/test_error_envelope.py` — `test_not_found_error_shape` | **PASS** |
| 6 | Retry-safe mutations (no row duplication) | Auth: `test_no_duplicate_row_on_conflict`; Social: `test_no_duplicate_row_on_conflict`; Posts: `test_no_duplicate_row_on_validation_failure`; Like: `test_retried_like_does_not_increase_count` | **PASS** |
| 7 | No committed hard-coded secrets | Config reads from env vars only. `TING_JWT_SECRET` and `TING_DEMO_PASSWORD` required at runtime. | **PASS** |
| 8 | No sensitive data in responses | `integration/test_error_envelope.py` — `test_register_response_no_password`, `test_login_response_no_password_hash`, `test_profile_response_no_password`, `test_error_response_does_not_leak_password_hash`, `test_token_value_not_logged_or_exposed_in_error` | **PASS** |
| 9 | Modules responsibility-oriented | api/, social.py, posts.py, interactions.py, auth.py, web/routes.py — clear separation. | **PASS** |
| 10 | Section 7 exclusions not implemented | No Docker, no image/video, no real-time, no search, no microservices, no native app. Only FastAPI + SQLite + Jinja2. | **PASS** |

**AC7 Verdict: PASS** (10/10 criteria verified)

---

### AC8 — TT-TEST (Regression Evidence)

| Command | Exit | Tests | Duration | Skipped | Status |
|---|---|---|---|---|---|
| `pytest tests/unit` | 0 | 63 | ~17s | 0 | **PASS** |
| `pytest tests/integration` | 0 | 219 | ~118s | 0 | **PASS** |
| `pytest` (full) | 0 | **282** | ~136s | 0 | **PASS** |

**Notes:**
- All original tests from t-001 through t-005 are present and passing.
- 9 new tests added in `tests/integration/test_smoke_coverage.py` to fill identified gaps (8 original + 1 CLI init→seed).
- Zero tests were removed, skipped, weakened, or had assertions relaxed.
- One product file modified: `ting_ting/main.py` — `main()` now calls `validate_and_initialize_schema()` so `python -m ting_ting` actually creates schema.
- Total 282 tests: 63 unit + 219 integration.

**AC8 Verdict: PASS**

---

## 2. Gap-Filling Tests Added

The following test gaps were identified during the coverage audit and filled with new black-box tests:

| Gap ID | Scope | Test Class | Tests Added | Reason |
|---|---|---|---|---|
| GAP1 | TT-FEED | `TestGap3UserFeedIsolation` (2 tests) | 2 | No test for 3-user feed isolation (Carol's FRIENDS post hidden from Alice/Bob's feed). Required by scope §4.3. |
| GAP2 | TT-WEB | `TestGapRejectRequestWeb` (1 test) | 1 | Reject friend request via web form had no dedicated web test. |
| GAP3 | TT-WEB | `TestGapUnblockWeb` (1 test) | 1 | Unblock via web form had no dedicated web test. |
| GAP4 | TT-WEB | `TestGapFeedAfterLogout` (2 tests) | 2 | Feed page access after cookie logout was not verified at the web layer. |
| GAP5 | TT-WEB | `TestGapEmptyContentErrorVisible` (2 tests) | 2 | Empty post content error message visibility in HTML not verified. |
| GAP6 | TT-CLI | `TestGapCLIInitAndSeed` (1 test) | 1 | `python -m ting_ting` CLI entry point (no args) never tested for black-box schema init + seed end-to-end. |

**Total new tests: 9**

---

## 3. Browser Smoke Evidence

Real browser smoke test executed with Playwright headless Chromium against a seeded local server (env-generated secrets, fresh DB):

- **30 direct browser checks: 30 PASS, 0 FAIL, 0 INCONCLUSIVE**
- **Fixes applied to smoke script (docs/smoke_browser_final.py):**
  - Check 4 (short username validation): switched from `page.click()` + timeout to `page.evaluate('form.submit()')` + `expect_navigation` + `wait_for_selector('.alert-error')` — resolves stale DOM race condition.
  - Check 24 (Enter login): fresh `/login` navigation, explicit `page.focus('button[type="submit"]')`, and `expect_navigation` — replaces unreliable `page.keyboard.press("Enter")` on potentially dirty page state.
  - Check 13 (Bob sees FRIENDS post): assertion tightened from generic `"post" in html` to `"hello friends" in html.lower()` (Alice's specific FRIENDS content: "Hello friends! Alice here and this is visible to my friends.").
  - `main()` exits `sys.exit(1)` on any FAIL or INCONCLUSIVE result; exits 0 only on all PASS.
- **16 fresh screenshots** captured at desktop (1280×900) and mobile (375×812) viewports
- All 52 checklist items marked verified (✓)
- Test evidence linked for each item
- 52/52 items covered by automated tests or browser verification

See `docs/BROWSER_SMOKE_EVIDENCE.md` for full check-by-check results and `docs/BROWSER_SMOKE_CHECKLIST.md` for the master checklist.

---

## 4. Seed Verification

```
$ python -m ting_ting
Database: sqlite:////tmp/test_seed2.db
Schema validation & initialization passed.
exit: 0

$ python -m ting_ting seed
Preflight passed — schema compatible, database empty.
Seed completed successfully.
  Users: alice, bob, carol
  Friendship: alice <-> bob
  Posts: alice (FRIENDS + ONLY_ME), bob (FRIENDS), carol (FRIENDS)
  Like: bob -> alice's FRIENDS post
  Comment: bob on alice's FRIENDS post
exit: 0

$ python -m ting_ting seed  (second invocation)
ERROR: Target database already contains data. No mutation was performed.
exit: 1

Data verification:
  Users: 3 | Friendships(accepted): 1
  FRIENDS posts: 3 | ONLY_ME posts: 1
  Likes: 1 | Comments: 1
  Usernames: ['alice', 'bob', 'carol']
```

---

## 5. Verdict

**OVERALL: PASS**

All 8 acceptance criteria (AC1–AC8) verified. Full regression: 282 tests, 0 failures, 0 skips. Browser smoke: 30 direct checks (30 PASS, 0 FAIL), 52/52 checklist items verified, fresh screenshots at desktop + mobile. Smoke script: exits nonzero (1) on FAIL/INCONCLUSIVE; check 13 asserts Alice's specific FRIENDS content (`hello friends`). CLI `python -m ting_ting` schema-init verified by black-box GAP6 test. Fresh seed: 3 users + relationships + content + interactions, double-seed refused. One product file modified (`ting_ting/main.py` — `main()` calls `validate_and_initialize_schema()`). 9 new black-box tests added to fill identified coverage gaps.