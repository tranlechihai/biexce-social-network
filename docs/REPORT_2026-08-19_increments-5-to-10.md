# Báo cáo giao tiếp — Increments 5–10 (Biexce Social Backend)

Ngày: 2026-08-19 · Môi trường: dev local (Python 3.14, FastAPI, SQLite, systemd user service `biexce-social-user.service` :8080)
Nguồn sự thật: `PROJECT_STATUS_AND_ROADMAP.md` · Baseline kiểm chứng: **568 passed / 0 failed**, ruff xanh

## 1. Outcome

**VERDICT: 6/6 increments đã lên kế hoạch trong phiên này HOÀN TẤT** (Safety & Moderation, Auth Lifecycle, Production Hardening P2.1–3, Upload Hardening P0.4, API Versioning, UI Polish). Toàn bộ test + lint xanh, chạy live trên :8080, migration dev DB tới `0007`. Không có criterion nào bị bỏ qua không lý do. Rủi ro còn lại tập trung ở PostgreSQL (chưa cutover) và các mục blocked bởi email provider.

## 2. Tasks / criteria

| # | Increment | Status | Evidence |
|---|---|---|---|
| 5 | Safety & Moderation — report idempent + audit, ban/unban (freeze/sever/auto-resolve), mod delete, mod UI | DONE | migration `0005`; 38 test `test_moderation.py` (19 unit + 19 integration); live check 20/20 (phiên trước) |
| 6 | Auth Lifecycle — server-side session, refresh/logout/logout-all, change-password parity, revoke sessions | DONE | migration `0006`; 12 unit + 9 integration (`test_sessions.py`, `test_auth_lifecycle.py`); live 16/16 (`.tmpdbg/live_check6.py`) |
| 7 | Production Hardening — `/health`, `/ready`, `/metrics`, X-Request-ID + access log, `requirements.lock`, ruff, CI, backup/restore + runbook | DONE | 15 test (`test_metrics.py`, `test_observability.py`); `scripts/backup.sh` chạy thật + verify restore trên copy (integrity ok, alembic 0006, 36 users); `curl /ready` → 200; journal access log `rid=... -> 200 x.xx ms` |
| 8 | P0.4 Upload — quota per-user + fleet, scan blocked content trong buffer, CSP baseline | DONE | 12 unit + 8 integration (`test_uploads.py`, `test_upload_quota.py`); live: zip-nhét-PNG → 422 `blocked_content`, shebang → 422, PNG hợp lệ → 201; CSP header verify bằng curl |
| 9 | API Versioning — `/api/v1` canonical + `/api` deprecated (`Deprecation`/`Warning` header), OpenAPI cả 2 | DONE | 7 test (`test_api_versioning.py`); live: `curl -D /api/feed` → `deprecation: true` + `Warning: 299...`, `/api/v1/feed` sạch; OpenAPI 50 path mỗi generation |
| 10 | UI Polish — profile counts, upload preview/progress, confirm dialogs, feed errors rõ + migration `0007` FK cascade | DONE | 8 test (`test_ui_polish.py`); verify browser thật (profile "0 Đang theo dõi · 0 Người theo dõi"); xóa user → posts cascade (live trên dev DB) |

## 3. Thay đổi theo subsystem

- **`ting_ting/uploads.py`** (mới): validate magic bytes + scan PE/ELF/ZIP/PDF/Java/OLE2/shebang trong buffer + quota đo disk thật — single source của 4 ingest paths (API post media, web post media, avatar ×2).
- **`ting_ting/metrics.py`** (mới): counter in-process + latency histogram, render Prometheus text.
- **`ting_ting/sessions.py`**, **`ting_ting/moderation.py`** (từ inc 5–6): session registry; moderation service.
- **`ting_ting/main.py`**: middleware request-ID/metrics/access-log + deprecation notice + CSP header; endpoints `/health`, `/ready`, `/metrics`; dual-mount `/api` + `/api/v1`.
- **`ting_ting/api/*`** (11 file): prefix tách khỏi `/api` (cho dual-mount); `auth.py` thêm refresh/logout-all/change-password; `moderation.py` (8 endpoints); sửa 2 bug F821 thật (`post`/`post_id` undefined trong media response paths) + import `IntegrityError` thiếu (web follow).
- **`ting_ting/web/routes.py`**: upload dùng shared validator/quota, feed render mã lỗi, profile counts, avatar 2 handlers gộp về 1 helper.
- **`ting_ting/models.py`**: `AuthSession`, `Report`, `Mute` tổng quát, `Post.author_id` FK CASCADE.
- **`alembic/versions/`**: `0005` (safety), `0006` (sessions), `0007` (posts cascade) — 0003 có ghi chú downgrade chỉ an toàn DB rỗng.
- **`scripts/backup.sh`**, **`scripts/restore_sqlite.sh`**, **`docs/RUNBOOK.md`** (mới).
- **`.github/workflows/ci.yml`**, **`requirements.lock`** (mới); **`pyproject.toml`**: ruff config.
- **Templates/static**: `feed.html` (composer preview/progress, confirm dialogs), `profile.html` (stats, confirm), `style.css` (`.profile-stats`, `.composer-preview*`), cache param `?v=nodi-12`.
- **Tests**: 15 file liên quan, +117 test từ phiên (451 → 568).

## 4. Verification (commands + kết quả)

| Check | Command | Kết quả |
|---|---|---|
| Full suite | `.venv/bin/python -m pytest -q` | **568 passed, 0 failed** (~4:50) |
| Lint | `.venv/bin/ruff check ting_ting tests` | All checks passed (0) |
| Migration dev | alembic upgrade head (subprocess, TING_DATABASE_URL dev) | `alembic_version = 20260819_0007`, posts có `ON DELETE CASCADE` |
| Service live | `systemctl --user is-active biexce-social-user.service` | active |
| Health/ready | `curl /health`, `curl /ready` | 200 `{status: ok}` / 200 `{status: ready, database: ok}` |
| Metrics | `curl /metrics` | 2xx counter + histogram render đúng |
| CSP | `curl -D - /web/login` | CSP header đầy đủ |
| Deprecation | `curl -D - /api/feed` vs `/api/v1/feed` | header chỉ trên `/api` |
| Scan upload | 3 POST multipart (zip-in-PNG, shebang, PNG) | 422 / 422 / 201 |
| Restore backup | script backup → verify file snapshot | integrity ok, alembic 0006, 36 users |
| Browser | `/web/profile/me` user mới | render stats 0/0 đúng |

Skipped: test trên PostgreSQL thật (chưa có instance hợp lệ), browser E2E cho thanh progress XHR (chỉ verify DOM hook + logic qua integration), screen-reader audit.

## 5. Failure / blocker

| Vấn đề | Loại | Trạng thái |
|---|---|---|
| PostgreSQL cutover (0002–0007 chưa chạy trên PG thật, chưa đo migration lock) | infra — cần quyền admin tạo role/DB | **BLOCKED**, chờ thông tin đăng nhập admin (RUNBOOK §7) |
| Forgot-password + email verification | dependency — chưa có email provider | Deferred, có thiết kế trong roadmap |
| pip-audit trong CI | environment — CI cần mạng (repo chưa có GitHub) | Deferred |
| mypy toàn phần | effort typing toàn bộ codebase | Deferred (ruff F/B/E đang giữ gate chính) |
| Bug thật phát hiện & sửa trong phiên: 2× F821 media paths, `select/func` thiếu cho moderator badge (im lặng từ inc 2 do try/except), `posts.author_id` thiếu cascade (0007) | code — pre-existing | Đã sửa, có test |

## 6. Decision / waiver

- **CSP giữ `'unsafe-inline'` cho script/style** — template hiện dùng inline JS (Nodi style); waiver có chủ đích, gắn điều kiện tighten bằng nonce khi đưa JS ra file (ghi trong comment `main.py` + roadmap).
- **Rate limiter process-local** — pre-existing; không làm trong scope này, ghi rõ là gate trước khi scale đa worker.
- **`/api` legacy giữ song song** (không gỡ) — policy deprecation: header + ghi chú "gỡ ở major version tới".

## 7. Known gaps / residual risk

- **PG path các migration 0002–0007 chưa chạy thật** — code viết theo SQLAlchemy/PG syntax nhưng chưa verify trên PostgreSQL; đây là rủi ro lớn nhất trước production (0003 downgrade còn hạn chế DB rỗng).
- Rate limit không toàn cục khi chạy >1 worker; quota upload đo disk từng request (đủ cho scale hiện tại, cần recount/summary khi rất lớn).
- Upload progress chỉ verify DOM + logic, chưa E2E browser thật.
- i18n vẫn lẫn tiếng Anh ở một số nút (Unfriend/Block/Thread tabs); screen-reader audit thủ công chưa làm.
- Không phải git repo → CI workflow chưa chạy ở đâu cả cho đến khi push; `requirements.lock` chưa được CI dùng. *(Cập nhật T-012: CI giờ cài từ `requirements.lock` + `requirements-dev.lock` + `pip install --no-deps -e .`; lock còn chờ lần chạy CI thực trên GitHub.)*

## 8. Next actions

| # | Hành động | Owner | Điều kiện bắt đầu |
|---|---|---|---|
| 1 | PostgreSQL cutover (ROLE/DB → alembic head trên PG → import/seed → test → đổi `.env` → restart) | Developer | Có quyền admin PG (RUNBOOK §7) |
| 2 | Push repo lên GitHub → bật CI (ruff + pytest) + thêm pip-audit | Developer | Có repo remote |
| 3 | Chọn email provider (Resend/SES/SMTP nội bộ) → implement forgot-password + verification | PM quyết định provider | Quyết định vendor |
| 4 | mypy baseline (strict dần) | Developer | Sau khi CI xanh |
| 5 | Screen-reader audit thủ công + dọn i18n | QA | Trước gate public |
| 6 | Mentions `@user` (design linkify + notification) | PM + Developer | Sau khi UI ổn định |