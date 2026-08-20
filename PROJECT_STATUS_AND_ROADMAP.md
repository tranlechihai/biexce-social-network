# Biexce Social — Trạng Thái Hiện Tại & Roadmap

Cập nhật: 2026-08-20. Nguồn sự thật: source code trong repo.
Increments đã hoàn thành: Core Integrity → … → API Versioning → UI Polish →
audit/rewrite migration 0007 (T-011) → lockfile/CI portable (T-012) →
backup/restore drill 0007 (T-014) → accessibility + i18n + browser upload E2E (T-017)
→ DB cleanup + pip-audit/CI audit job (T-018 local).
Baseline hiện tại: **570 passed / 0 failed** (392 integration + 178 unit), ruff xanh.
Quyết định 2026-08-20: **tiếp tục chạy SQLite**; các task PostgreSQL
(T-015/T-016) ở trạng thái **PENDING** cho đến khi có thông tin PG.

## 1. Tổng quan kiến trúc

Single deployable modular monolith:

- Python 3.14, FastAPI (REST JSON + web server-rendered)
- SQLAlchemy 2 (ORM) — SQLite cho development, PostgreSQL + Alembic cho server
- Jinja2 + HTML/CSS/JS thuần cho web demo (không framework frontend)
- Pytest: unit + integration
- Frontend là server-rendered Jinja2; API JSON dành cho client tương lai (mobile/app)

Bản đồ module:

| Module | Trách nhiệm |
|---|---|
| `ting_ting/main.py` | App factory, mount router/static/uploads, lifecycle |
| `ting_ting/config.py` | Config từ env prefix `TING_` |
| `ting_ting/database.py` | Engine/session, khởi tạo schema create-only |
| `ting_ting/models.py` | ORM: User, UserProfile, FriendRequest, Block, Post, Like, Comment, Follow, Activity, SavedPost, Repost, PostMedia, Mute, Report, AuthSession |
| `ting_ting/auth.py` | bcrypt, JWT, cookie, auth dependencies |
| `ting_ting/keyset.py` | Keyset cursor (created_at/id + key/id) cho feed/comments/user search |
| `ting_ting/social.py` | Chính sách friendship/block/mute/search (single source of truth) |
| `ting_ting/posts.py` | Post CRUD, audience visibility, feed query (SQL filter + mute/hidden suppression) |
| `ting_ting/interactions.py` | Like idempotent, comment (reply 1 mức + edit) |
| `ting_ting/moderation.py` | Report (dedup/visibility), resolve (audit), ban/unban (sever + auto-resolve), mod delete content |
| `ting_ting/sessions.py` | Server-side session lifecycle: create, get_active, revoke, revoke_all (keep) |
| `ting_ting/metrics.py` | In-process counters + latency histogram, render Prometheus text (`/metrics`) |
| `ting_ting/uploads.py` | Upload validation (magic bytes), scan nội dung bị cấm trong buffer, quota per-user/fleet — dùng chung 4 ingest paths |
| `scripts/backup.sh`, `scripts/restore_sqlite.sh` | Backup/restore DB (snapshot online) + uploads, retention 10 |
| `docs/RUNBOOK.md` | Runbook: deploy, rollback, backup/restore, observability, sự cố thường gặp |
| `ting_ting/api/` | REST endpoints (`/api/...`) — gồm `moderation.py` (reports/bans/mod delete) |
| `ting_ting/web/` | Web demo Jinja2 (`/web/...`) |
| `ting_ting/seed.py` | Seed transactional, fresh-only |

## 2. Chức năng hiện có

### Backend REST (`/api`)

| Nhóm | Trạng thái | Ghi chú |
|---|---|---|
| Auth (register/login/logout) | ✅ V2 | JWT ngắn hạn + **server-side session** (`/api/auth/refresh`, `/logout` revoke session, `/logout-all`, `change-password` API parity — revoke mọi session khác); logout thật (token chết dù cookie còn), bcrypt |
| Profile (GET/PATCH `/api/profile/me`) | ✅ MVP cơ bản | Chỉ display_name + bio |
| Friend request (send/list/accept/reject) | ✅ MVP | Canonical pair, unique active request |
| Friends (list/unfriend), relationship state | ✅ MVP | `GET /api/social/relationship/{id}` |
| Block/unblock/list | ✅ MVP | Bilateral, không restore khi unblock |
| Posts CRUD + feed | ✅ V2 | Feed "for you" + `/api/feed/following` (posts + public reposts), visibility lọc trong SQL, keyset cursor (`X-Next-Cursor`), offset legacy vẫn chạy. **Tất cả endpoint API có 2 generation: `/api/v1/...` (canonical) + `/api/...` (legacy, deprecated — response mang `Deprecation`/`Warning` header)** |
| Like/unlike (idempotent), comment CRUD | ✅ V2 | Comment reply 1 mức (`parent_comment_id`, reply-of-reply → 422), edit comment (tác giả, PATCH), delete cascade replies; like/comment/reply đều tạo notification (reply notify cả tác giả comment mẹ) |
| Follow/activity/saved/repost/media | ✅ API parity | Auth, ownership/visibility, block precedence và idempotency (savepoint) |
| Notifications (`/api/notifications`) | ✅ V2 | List + filter `kind` + cursor keyset, `unread-count`, `/{id}/read`, `read-all`; actor bị block **hoặc mute** bị ẩn (kể cả unread-count); dedup khi retry |
| Users (`/api/users`) | ✅ Mới | Search `?q&cursor` (keyset `username,id`, loại viewer + người block mình), `GET /{username}` public profile (relationship + follower/following/friend counts, block → redact), `GET /{username}/followers\|/following` (blocked pair → 404) |
| Mute / ẩn bài | ✅ V2 | `PUT\|DELETE /api/social/mutes/{id}` (user mute, không đụng relationship), `PUT\|DELETE /api/posts/{id}/hidden` (post mute — khỏi cả 2 feed, đọc trực tiếp vẫn được); cùng bảng `mutes` (2 dạng user/post, partial unique index) |
| Hủy friend request đã gửi | ✅ Mới | `DELETE /api/social/requests/{id}` — chỉ người gửi, phải còn pending |
| Reports (`/api/reports`) | ✅ Mới | `POST /api/reports` (post/comment/tài khoản, idempotent, visibility enforced), `GET /api/reports` + `/{id}/resolve\|/dismiss` (moderator-only, audit: resolved_by + note); auto-resolve khi ban target |
| Ban / Unban | ✅ Mới | `POST /api/social/bans` + `DELETE /api/social/bans/{id}` (moderator-only): freeze (login 403 `banned`, API 401), ẩn khỏi discovery + feed, sever follows/requests, idempotent |
| Mod content removal | ✅ Mới | `DELETE /api/mod/posts/{id}`, `DELETE /api/mod/comments/{id}` (moderator-only, cascade) |

### Frontend web (`/web`)

| Nhóm | Trạng thái | Ghi chú |
|---|---|---|
| Register/Login/Logout | ✅ V2 | Auto-login sau register, error hiển thị trên trang; logout revoke session server-side; đổi mật khẩu kết thúc session khác; nút "Đăng xuất khỏi mọi thiết bị" trong trang cá nhân |
| Profile counts + upload UX + confirm dialogs | ✅ | "Đang theo dõi / Người theo dõi" trên profile (redact khi bị block); composer preview ảnh/video + progress % khi upload + thông báo lỗi rõ; confirm trước khi xóa bài/comment, block, unfriend |
| Feed + composer | ✅ | Mới nhất trước, audience select, nút like/comment |
| Media trong post (ảnh/video ≤25MB) | 🧪 Beta web-only | Lưu filesystem `uploads/`, delivery qua endpoint có authorization |
| Edit/delete post của mình | ✅ | Form inline trong feed |
| Profile cơ bản + mở rộng (birthday, gender, location, occupation, website, avatar upload nhanh) | ✅ Web-only | Đổi mật khẩu web; API có extended profile; blocked profile redact field riêng tư + post |
| Người dùng (people search) | ✅ | Tìm theo username/display_name; nút mute (Giấu/Đang giấu) |
| Social actions (request/accept/reject/unfriend/block/unblock) | ✅ | Qua form trên profile; hủy request đã gửi ("Hủy lời mời" khi pending_outgoing) |
| Comment reply + edit | ✅ | Trả lời 1 mức dưới comment (form mở bằng "Trả lời"), sửa comment của mình ("Sửa"); hiển thị ↩ tên người được trả lời |
| Mute / ẩn bài | ✅ | Mute user từ people + profile; nút "Ẩn" che bài khỏi feed (toggle API) |
| Báo cáo nội dung | ✅ | Nút ⚑ trên bài viết + comment (5 lý do), toast xác nhận sau gửi |
| Hàng đợi điều phối viên (`/web/mod/reports`) | ✅ | Tab đang xử lý/đã xử lý/bỏ qua, ghi chú audit, nút khóa tài khoản; nav "Báo cáo" + badge chỉ với moderator |
| Trang tài khoản tạm khóa | ✅ | User bị ban: web render trang thông báo (403), login hiển thị lỗi, API 401/403 `banned` |
| Follow/unfollow | ✅ | Kiểm tra block tại mutation, idempotent; có API parity |
| Activity page | ✅ | follow/like/comment/repost, read/unread, mark-read + read-all, unread badge nav; API cursor pagination |
| Saved posts | 🧪 Beta | Toggle save, tái kiểm tra visibility khi render |
| Repost | 🧪 Beta | Toggle, counter, chưa có API |
| Responsive + dark mode | ✅ | Viewport mobile/desktop, toggle theme localStorage |

### Kiểm thử

- Baseline hiện tại (2026-08-20, full suite): **570 passed, 0 failed, 0 skipped** (568 + 2 test round-trip/fail-closed của audit migration 0007, T-011).
- Gồm unit (policy social, visibility, interaction, auth, notification service, social interaction, moderation, session service, metrics, error envelope) + integration (REST + web + notifications + social interaction + moderation + auth lifecycle + observability + seed + schema init + alembic migration + smoke coverage).
- Browser E2E (Chromium thật qua Playwright): `docs/upload_e2e.py` — composer upload (preview/gỡ, XHR progress, render media, chặn ZIP nhét đuôi ảnh, chặn >25MB), desktop + mobile; chạy trên dev server đang live.
- Chạy: `.venv/bin/python -m pytest`; lint: `.venv/bin/ruff check ting_ting tests` (xanh, config trong `pyproject.toml`).

## 3. Dữ liệu và file

| Thứ | Vị trí local | Env key |
|---|---|---|
| Database | Local mặc định `./ting_ting.db`; server mục tiêu PostgreSQL `127.0.0.1:5432/biexce_social` | `TING_DATABASE_URL` |
| Upload (avatar/media) | `./uploads/` | Không cấu hình được — hard-code `ting_ting/web/routes.py` |
| JWT secret | Env, bắt buộc | `TING_JWT_SECRET` |
| Demo password (seed) | Env, bắt buộc khi seed | `TING_DEMO_PASSWORD` |
| Cookie Secure flag | Env, server phải `true` khi HTTPS | `TING_COOKIE_SECURE` |
| JWT expiry | Env (phút, mặc định 60) | `TING_JWT_EXPIRE_MINUTES` |
| Quota upload per user | MB, mặc định 512 | `TING_UPLOAD_QUOTA_MB` |
| Quota upload toàn hệ thống | MB, mặc định 5120 | `TING_TOTAL_UPLOAD_QUOTA_MB` |

Test dùng database tạm/in-memory, không đụng DB chính.

## 4. Rủi ro và khoảng trống hiện tại

### P0 — trạng thái security phase 1

1. **Đã xử lý:** media local được phục vụ qua `/media/{filename}` với authentication và kiểm tra visibility hiện tại; `/uploads/{filename}` chỉ là route tương thích có cùng authorization.
2. **Đã xử lý:** mọi mutation `/web` yêu cầu double-submit CSRF token.
3. **Đã xử lý:** login/register/upload và mutation API/web có process-local rate limit, trả `429` khi vượt giới hạn.
4. **Đã xử lý:** file media được cleanup khi xóa post, file mới được cleanup khi transaction thất bại, avatar cũ được cleanup khi thay thế. Quota per-user + fleet (`TING_UPLOAD_QUOTA_MB` / `TING_TOTAL_UPLOAD_QUOTA_MB`) + scan nội dung bị cấm (PE/ELF/ZIP/PDF/OLE2/shebang trong buffer) đã có (P0.4).
5. **Đã xử lý:** follow/activity tuân thủ block policy; like replay không tạo activity trùng.

### P1 — production readiness

1. **Đã có migration framework:** Alembic baseline quản lý schema PostgreSQL; startup PostgreSQL chỉ validate, không tự chạy DDL.
2. **Đã có đường chuyển SQLite → PostgreSQL:** target phải rỗng, copy transaction, giữ ID, verify count và reset sequence.
**Đã xử lý:** mọi FK của post (likes/comments/saved/reposts/media/activities) có `ON DELETE CASCADE`; SQLite enforce FK per-connection (event listener); xóa post không để orphan (có test). Check constraints `ck_follow_not_self`, `ck_post_audience`, `ck_activity_kind`, state/canonical của friend request.
5. **Đã xử lý:** accept friend request recheck block tại thời điểm accept; API follow/save/repost idempotent qua savepoint (concurrent duplicate không còn 500); profile của pair bị block bị redact bio/contact/avatar/posts.
6. Rate limiter hiện process-local; chưa thể tăng nhiều worker mà vẫn giữ quota toàn cục chính xác.
7. **Đã xử lý (Auth Lifecycle):** auth có refresh/revocation (server-side session, migration 0006). Còn: quên mật khẩu (cần email provider), email verification.
8. **Đã xử lý (Production Hardening):** có `/health`, `/ready`, access log + request ID, `/metrics`, CI workflow cài từ lock (`requirements.lock` + `requirements-dev.lock`, T-012) (xem docs/RUNBOOK.md).
9. **Đã xử lý:** feed for-you + following đã lọc SQL + batch N+1, repost public là feed candidate (xem Giai đoạn 3).
10. **Đã xử lý (T-017):** nhãn web còn lẫn tiếng Anh (Unfriend/Block/Unblock/Send Friend Request/Thread/Relationship/Friends + toàn bộ validate error register/login/profile/avatar) đã Việt hóa; upload media qua XHR giờ nhận JSON 422 + hiện lý do từ server trên composer (trước đó XHR theo 303 redirect nên **mất** thông báo lỗi). Còn: screen-reader audit thủ công với NVDA/VoiceOver thật (audit tĩnh + browser E2E đã làm).

## 5. Roadmap

### Giai đoạn 1 — Bảo mật (P0) · PARTIAL
- [x] CSRF token cho tất cả form mutation web.
- [x] Process-local rate limit: login, register, post, comment, upload, social actions.
- [x] **Upload quota + scan + CSP** (P0.4, `ting_ting/uploads.py` — single source of truth cho cả 4 ingest paths API/web post media + avatar 2 chỗ):
  - Quota theo người (`TING_UPLOAD_QUOTA_MB`, mặc định 512) + quota toàn hệ thống (`TING_TOTAL_UPLOAD_QUOTA_MB`, mặc định 5120) đo trên dung lượng thật của `uploads/`; vượt → API 413 `quota_exceeded`/`storage_full`, web hiện thông báo rõ trên feed/profile (không còn redirect im lặng).
  - Scan nội dung bị cấm: reject executable (PE/ELF), ZIP, PDF, Java class, OLE2 tìm **sâu trong buffer** (bắt được payload nhét sau header PNG hợp lệ), shebang ở đầu file → `blocked_content` (API 422, web toast).
  - Consolidate magic-byte validation (trước đó nhân bản 3 nơi, avatar thiếu check WEBP thật).
  - CSP header mọi response: `default-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`… (inline JS/style vẫn dùng nên giữ `'unsafe-inline'` cho script/style — tighten bằng nonce khi đưa JS ra file ngoài).
- [x] Media private qua endpoint kiểm tra authorization hiện tại.
- [x] Block policy cho follow/activity; deduplicate like Activity.
- [x] Cleanup media khi xóa post, rollback upload và thay avatar.
- [x] Security headers cơ bản: X-Content-Type-Options, Referrer-Policy, X-Frame-Options.
- [x] Integration tests: CSRF reject, private media, rate limit 429, follow-when-blocked, cleanup.
- [x] Quota upload per user/total (`TING_UPLOAD_QUOTA_MB`/`TING_TOTAL_UPLOAD_QUOTA_MB`), scan nội dung bị cấm trong buffer, CSP baseline phù hợp inline script hiện tại (P0.4 — `ting_ting/uploads.py`).

### Giai đoạn 2 — PostgreSQL + migration · PARTIAL
- [x] `psycopg[binary]`, PostgreSQL runtime và Alembic baseline full schema.
- [x] Script SQLite → PostgreSQL: fail-closed, transaction, giữ ID, verify count, reset sequence.
- [x] systemd + Nginx artifact cho `10.100.0.46`.
- [x] Migration `20260818_0002`: `activities.read_at`, `ON DELETE CASCADE` cho mọi FK của post, check constraints (self-follow, audience, activity kind, friend-request state/canonical). SQLite dùng recreate 12-step thủ công (giữ partial index); đã chạy trên DB dev thật + round-trip downgrade/upgrade.
- [x] Migration `20260818_0003` (index `ix_posts_created_at_id`) + `20260818_0004` (comment `parent_comment_id` self-FK cascade, bảng `mutes`, `hidden_posts`).
- [x] Migration `20260819_0005` (Safety & Moderation): `users.is_moderator` + `banned_at`, bảng `reports` (FK SET NULL để audit sống qua xóa nội dung), `mutes` tổng quát hóa (user OR post, partial unique index, fold `hidden_posts` rồi drop).
- [x] Migration `20260819_0006` (Auth Lifecycle): bảng `sessions` (registry server-side cho JWT ngắn hạn, user FK CASCADE).
- [x] Migration `20260819_0007` (UI Polish): `posts.author_id` FK `ON DELETE CASCADE` (SQLite rebuild bảng; PG đổi constraint in-place). DB dev đã upgrade tới 0007.
- [x] **Audit/rewrite 0007 (T-011):** orphan preflight fail-closed trước DDL; rebuild bảng atomic bằng `transactional_ddl=True` + `transaction_per_migration=True` trong `alembic/env.py` (mọi migration chạy trong 1 DB transaction); downgrade giữ `ck_post_audience`; 2 test round-trip + fail-closed. So sánh backup pre-0007 xác nhận không mất dữ liệu.
- [ ] Chạy migration thật cần PostgreSQL credential/role/database trên server (PostgreSQL path của 0002 đã viết, chưa chạy thật).
- [ ] Đo migration lock trước production.
- [ ] Tối ưu index theo query plan PostgreSQL thực tế.
- [ ] Distributed rate limit trước khi tăng nhiều app worker.

### Giai đoạn 3 — Backend API parity · PARTIAL
- [x] Extended profile GET/PATCH với validation URL, birthday, gender và giới hạn độ dài.
- [x] Follow/unfollow idempotent, followers/following, block precedence và cleanup edge.
- [x] Activity list có authorization và pagination `limit/offset`.
- [x] Saved post/repost idempotent, kiểm tra visibility tại read/mutation path.
- [x] Post media upload/delete có ownership, magic-byte validation và cleanup file.
- [x] Notifications API: list (cursor keyset + filter kind), unread-count, mark read, read-all — dùng chung service `ting_ting/notifications.py` cho cả web và API (parity like/comment/follow/repost).
- [x] Feed V2: visibility lọc trong SQL (không còn load-all + lọc Python), keyset cursor cho `/api/feed` và comments (offset legacy giữ, deprecated), `/api/feed/following` (posts của người được follow + public posts do họ repost), PostResponse có `media`, `repost_count`, `saved_by_viewer`, `reposted_by_viewer`; feed web batch group-by (loại N+1 author/media/counts/viewer-state/comments); index `ix_posts_created_at_id` (migration 0003).
- [x] Social Interaction V1: comment reply 1 mức + edit comment + notification cho tác giả comment mẹ; hủy friend request đã gửi; `/api/users` search cursor + public profile (counts, block redact) + followers/following graphs; mute user (ẩn khỏi 2 feed + notifications, không đụng relationship); ẩn bài khỏi feed (migration 0004). **Mentions (`@user`) defer** — cần linkify + notification design, để increments sau.
- [x] Safety & Moderation: report (post/comment/tài khoản, idempotent, visibility enforced, dedup), hàng đợi moderator (list/resolve/dismiss + audit resolved_by/note + auto-resolve khi ban), ban/unban (freeze auth API 401/web 403 `banned`, ẩn discovery + feed, sever follows + friend requests), mod delete post/comment. Báo cáo sống qua xóa nội dung (FK SET NULL) giữ audit (migration 0005).
- [x] Auth Lifecycle: server-side session (`sessions`), JWT ngắn hạn mang `sid` — logout/logout-all thật (token chết server-side), `/api/auth/refresh` re-mint token giữ session, POST `/api/auth/change-password` (parity web, validate + revoke mọi session khác, giữ session hiện tại); session expire 7 ngày, cookie expire 1h. **Password reset (forgot, cần email link) defer** — không có email provider; đổi mật khẩu (biết mật khẩu cũ) phủ trường hợp tự phục vụ.
- [x] **API versioning**: `/api/v1/...` canonical + `/api/...` legacy alias (dual-mount, OpenAPI document cả hai). **Deprecation policy**: response `/api/...` mang `Deprecation: true` + `Warning: 299 ... use /api/v1` (`/api/v1` sạch header); client mới phải dùng `/api/v1`; `/media` (file delivery) không version. Gỡ `/api` legacy ở major version tới.

### Giai đoạn 4 — Frontend hoàn thiện · PARTIAL
- [x] Visual system theo mẫu Nodi: dark-first, accent cam, sidebar, feed, right rail và mobile bottom nav.
- [x] Đồng bộ các nhãn chính sang tiếng Việt.
- [x] Tab “Đang theo dõi” hoạt động và vẫn áp dụng audience visibility.
- [x] Action icon có accessible name, focus-visible và reduced-motion.
- [x] Chromium desktop/mobile: không horizontal overflow, responsive rail/nav đúng breakpoint.
- [x] Activity read/unread: badge không đọc trên nav, đếm chưa đọc trang activity, nút "Đã đọc" từng dòng + "Đánh dấu tất cả đã đọc".
- [x] Social Interaction UI: comment reply/edit inline, mute user (people + profile), "Hủy lời mời" khi pending_outgoing, nút "Ẩn" bài khỏi feed.
- [x] Moderation UI: nút ⚑ báo bài viết + comment (5 lý do) + toast, trang hàng đợi `/web/mod/reports` (tabs + ghi chú audit + "Khóa tài khoản"), nav "Báo cáo" + pending badge chỉ cho moderator, trang "Tài khoản tạm khóa" (403) + lỗi login khi bị ban.
- [x] Auth Lifecycle UI: đổi mật khẩu có ghi chú "kết thúc các phiên khác", khối "Thiết bị đã đăng nhập" + nút "Đăng xuất khỏi mọi thiết bị"; logout revoke session server-side.
- [x] **UI Polish**: profile follower/following count (redact khi block); composer upload preview (ảnh/video + nút gỡ) + tiến trình upload (XHR + percentage, lỗi mạng/đăng rõ ràng); confirmation dialog cho xóa bài, xóa comment, block, unfriend; feed render rõ các mã lỗi upload (quota/blocked/too large/failed) thay vì redirect im lặng. *Screen-reader audit thủ công vẫn chưa làm.*
- [x] **A11y (T-017):** nav icon-only có `aria-label` (mobile mất tên accessible); `aria-current="page"` nav + tabs feed/mod/activity; theme toggle `aria-pressed`; badge unread có ngữ cảnh sr-only; label↔input ghi chú moderator; sửa bug CSS `.composer-preview[hidden]` bị `display:flex` ghi đè (preview không ẩn sau khi gỡ ảnh); nút comment mang số lượng trong aria-label.
- [ ] Screen-reader audit thủ công với NVDA/VoiceOver thật (audit tĩnh + Lighthouse + browser E2E Chromium đã làm).

### Giai đoạn 5 — Hiệu năng & vận hành · PARTIAL
- [x] Feed: visibility filter trong SQL, loại N+1 bằng grouped aggregate (Feed V2).
- [x] **Observability** (P2.1): `GET /health` (liveness), `GET /ready` (DB ping, 503 khi DB chết), middleware `X-Request-ID` (echo client header nếu có) + access log 1 dòng/request (`rid=... path -> status duration`), `GET /metrics` (Prometheus text, in-process: `http_requests_total{status_class}`, latency histogram ms, `auth_login_failures_total`).
- [x] **CI & chất lượng** (P2.2): lock deploy — `requirements.lock` (runtime, version-only) + `requirements-dev.lock` (dev/test), chứng minh install trên venv sạch (T-012); ruff config trong `pyproject.toml` + lint toàn repo **xanh** (dọn 246 findings — gồm 2 bug thực: `post`/`post_id` undefined trong media response paths); `.github/workflows/ci.yml` — jobs `lint` + `test` (ruff pin trong dev lock + pytest, cài từ 2 lock + `pip install --no-deps -e .`; chạy khi repo lên GitHub) + job **`audit` (T-018 local, 2026-08-20)**: `pip-audit --disable-pip --no-deps` trên cả 2 lock (audit đúng pins đã khóa, không re-resolve); 2 CVE waive có lý do bằng `--ignore-vuln`: **ecdsa 0.19.2 PYSEC-2026-1325** (CVE-2024-23342, Minerva timing P-256, CVSS 7.4, **không có fix upstream**, transitive của python-jose — app chỉ dùng HS256 JWT nên code path ECDSA không chạy) và **pytest 8.4.2 PYSEC-2026-1845** (CVE-2025-71176, tmpdir handling, local vector, dev/CI-only; fix ở pytest 9.0.3 vượt constraint `>=7.4,<9.0`, upgrade track riêng). `pip-audit` nằm trong `requirements-dev.lock` (re-resolve 10→33 pins). **mypy vẫn defer** (cần effort typing toàn phần).
- [x] **Backup/restore** (P2.3): `scripts/backup.sh` (SQLite snapshot online nhất quán + tar `uploads/`, giữ 10 bản; tự nhận URL PG → `pg_dump`), `scripts/restore_sqlite.sh` (verify backup → safety copy → stop/replace/start), đã verify restore trên copy **tại 0006** và **drill tại revision 0007 (T-014, 2026-08-20)** — `backup.sh` giờ nhận diện đúng URL SQLAlchemy `postgresql+psycopg://` (chuyển về `postgresql://` cho `pg_dump`; trước đó fall through sang nhánh SQLite): snapshot online `backup-20260820-101306.sqlite` + `uploads-20260820-101307.tar.gz` restore vào vị trí disposable — integrity ok, `foreign_key_check` 0 orphan, `alembic_version = 20260819_0007`, counts (36 users/18 posts/7 comments/5 sessions...), `posts.author_id ON DELETE CASCADE`, đủ media (post_media 2/2, uploads 120 files). Cron mẫu trong runbook.
- [x] **Runbook** (P2.3): `docs/RUNBOOK.md` — deploy, rollback (code + migration), backup/restore, quan sát, bảng sự cố thường gặp, biến môi trường, cutover PG, mở khóa moderator.
- Còn: index theo query plan PostgreSQL thực tế (sau cutover — **PENDING theo quyết định 2026-08-20 giữ SQLite**), mypy. (pip-audit đã xong — job `audit` CI; quota + scan upload đã xong ở P0.4.)

### Thứ tự và gate

1. Giai đoạn 1 → gate: mở public nội bộ.
2. Giai đoạn 2 → gate: scale, deploy đa worker.
3. Giai đoạn 3 + 4 (có thể song song) → gate: mobile app client có thể phát triển trên API.
4. Giai đoạn 5 → gate: production.

## 6. Chạy local

```bash
cd ~/workspace/biexce-social-backend-slim

# Tạo venv (chỉ lần đầu)
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"

export TING_JWT_SECRET="$(openssl rand -hex 32)"
export TING_DEMO_PASSWORD="mat-khau-demo-cua-ban"

.venv/bin/python -m ting_ting          # khởi tạo schema (create-only)
.venv/bin/python -m ting_ting seed     # CHỈ trên DB rỗng: alice/bob/carol
.venv/bin/uvicorn ting_ting.main:app --reload
```

Mở:
- Web: http://localhost:8000/web/feed
- API docs: http://localhost:8000/docs

> Lưu ý: chạy `uvicorn` trực tiếp báo "Command not found" là vì chưa dùng binary trong venv.
> Hoặc `source .venv/bin/activate` trước rồi mới gọi `uvicorn`.

Chạy test:

```bash
.venv/bin/python -m pytest            # full suite
.venv/bin/python -m pytest tests/unit -q
```

## 7. Chạy lên server Ubuntu nội bộ (10.100.0.46)

Mô hình: PostgreSQL `127.0.0.1:5432` ← Uvicorn `127.0.0.1:8080` ← Nginx `:80` → `http://10.100.0.46/web/feed`.
Mặc định `uvicorn` chỉ bind `127.0.0.1`; muốn truy cập từ máy khác phải qua Nginx (hoặc `--host 0.0.0.0` nhưng **không** khuyến nghị mở public).

### 7.1 Chuẩn bị

```bash
sudo apt update
sudo apt install -y nginx postgresql-client
cd ~/workspace/biexce-social-backend-slim
./.venv/bin/pip install -e ".[test]"
```

### 7.2 Tạo PostgreSQL role/database

```bash
psql -h 127.0.0.1 -U postgres -d postgres
CREATE ROLE biexce_social LOGIN;
\password biexce_social
CREATE DATABASE biexce_social OWNER biexce_social;
\q
```

Cần credential admin PostgreSQL. Không đặt password trực tiếp trong shell history.

### 7.3 Environment

Tạo `/etc/biexce-social.env`:

```dotenv
TING_DATABASE_URL=postgresql+psycopg://biexce_social:<URL_ENCODED_PASSWORD>@127.0.0.1:5432/biexce_social
TING_JWT_SECRET=<kết-qua-cua-openssl-rand-hex-32>
TING_JWT_EXPIRE_MINUTES=60
TING_COOKIE_SECURE=false
TING_RATE_LIMIT_ENABLED=true
TING_DEBUG=false
```

```bash
sudo chown root:chihai /etc/biexce-social.env
sudo chmod 640 /etc/biexce-social.env
```

- `TING_COOKIE_SECURE=false` chỉ cho HTTP nội bộ; khi bật HTTPS phải `true`.
- Sinh secret: `openssl rand -hex 32`.

### 7.4 Alembic và chuyển dữ liệu SQLite

```bash
set -a; source /etc/biexce-social.env; set +a
./.venv/bin/alembic upgrade head
./.venv/bin/python -m ting_ting.migrate_data --source sqlite:///./ting_ting.db
```

Target phải hoàn toàn rỗng; script từ chối ghi đè, giữ ID, verify count từng bảng và reset PostgreSQL sequence. Dừng app đang ghi SQLite trước khi copy và backup `ting_ting.db` trước thao tác.

Nếu không chuyển dữ liệu cũ, seed PostgreSQL rỗng sau Alembic:

```bash
export TING_DEMO_PASSWORD="<mat-khau-mau>"
./.venv/bin/python -m ting_ting seed
```

### 7.5 Systemd

Artifact: `deploy/systemd/biexce-social.service`.

Nếu không có sudo, dùng user service đã cung cấp:

```bash
systemctl --user link "$PWD/deploy/systemd/biexce-social-user.service"
systemctl --user enable --now biexce-social-user.service
systemctl --user status biexce-social-user.service
```

User service bind trực tiếp `0.0.0.0:8080`. Trên workstation hiện tại đã xác minh
`http://10.100.0.46:8080/web/login` và `/docs` trả HTTP 200. Do `Linger=no`, service
có thể dừng sau full logout/reboot; admin cần chạy `loginctl enable-linger chihai`
để user service tự chạy khi chưa đăng nhập.

```bash
sudo install -m 0644 deploy/systemd/biexce-social.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now biexce-social
sudo systemctl --no-pager --full status biexce-social
```

Giữ một worker cho tới khi rate limiter chuyển sang Redis/shared storage.

### 7.6 Nginx

Artifact: `deploy/nginx/biexce-social.conf`.

```bash
sudo install -m 0644 deploy/nginx/biexce-social.conf /etc/nginx/sites-available/biexce-social
sudo ln -sfn /etc/nginx/sites-available/biexce-social /etc/nginx/sites-enabled/biexce-social
sudo nginx -t && sudo systemctl reload nginx
```

### 7.7 Kiểm tra sau deploy

```bash
curl -sI http://127.0.0.1:8080/web/login | head -1     # expect 200
curl -sI http://10.100.0.46/web/login | head -1        # expect 200 từ máy khác mạng
sudo journalctl -u biexce-social -n 50 --no-pager
```

Mở browser: http://10.100.0.46/web/feed

### 7.8 Backup

- Trước cutover: backup SQLite bằng `.backup`, không copy file đang được ghi.
- Sau cutover: `pg_dump -Fc` PostgreSQL và backup `uploads/`.
- Lịch hàng ngày, giữ tối thiểu 7 bản và test restore định kỳ.

## 8. Checklist hoàn thiện dự án

- [x] P0.1 Media private through authorized endpoint
- [x] P0.2 CSRF tokens trên mọi web mutation
- [x] P0.3 Rate limit toàn bộ entry points nhạy cảm
- [x] P0.4 Upload quota (per-user 512MB + fleet 5120MB, config theo env) + scan nội dung bị cấm + CSP baseline (orphan cleanup đã xong từ trước)
- [x] P1.1 PostgreSQL + Alembic baseline + SQLite transfer tool + migration 0002 (cascades, checks, read_at)
- [x] P1.2 FK cascade xóa post không orphan + check constraints (còn tối ưu index theo query plan PG)
- [x] P1.3 API parity: profile mở rộng, follow, activity, saved, repost, media, notifications (cursor + read state)
- [x] P1.7 Social Interaction: comment reply/edit + notification, hủy request, user search cursor, public profile + counts, followers/following graphs, mute, ẩn bài (mentions defer)
- [x] P1.8 Safety & Moderation: report + hàng đợi moderator (audit), ban/unban (freeze + sever + auto-resolve), mod delete content, mod role, moderation UI (migration 0005)
- [x] P1.4 Feed SQL + loại N+1 (Feed V2 — for-you + following + cursor)
- [x] P1.5 Auth: server-side session + refresh/logout/logout-all semantics, đổi mật khẩu API parity + revoke session (reset forgot-password defer — cần email provider) (migration 0006)
- [x] P1.6 Frontend: activity read/unread + badge, thread-new & activity pages, avatar nhanh, đổi mật khẩu, profile counts, upload preview/progress, confirmation dialogs, errors rõ (i18n nhất quán — T-017; screen-reader audit thật NVDA/VoiceOver vẫn còn)
- [x] P2.1 /health, /ready, request-ID + access log, metrics (Prometheus text)
- [x] P2.2 requirements.lock + ruff xanh + CI workflow + pip-audit job `audit` (T-018 local; 2 CVE waive ghi lý do) (mypy defer)
- [x] P2.3 scripts/backup.sh + restore_sqlite.sh (restore verified) + docs/RUNBOOK.md
