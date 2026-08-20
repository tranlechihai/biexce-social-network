# HANDOFF — biexce-social-backend-slim

Cập nhật: sau DB cleanup + T-018 phần local (pip-audit/CI audit job). Không chứa secret/key.

## 1. Mục tiêu hiện tại
**T-012, T-013, T-014, T-017 — ĐÃ XONG. T-018 — ĐÃ XONG PHẦN LOCAL**
(lock pip-audit + CI `audit` job + `.gitignore`); phần git init/commit/push
do human làm (policy git-flow-ai: agent không có quyền Git write) + cần
Git remote. **T-015/T-016 — PENDING** theo quyết định user 2026-08-20:
**vẫn chạy SQLite**, chưa chuyển PostgreSQL — chờ thông tin PG.

Baseline test hiện tại: **570 passed / 0 failed** (392 integration + 178 unit;
T-017 có sửa code app — full suite chạy lại xanh, không đổi số test).
Lint (`ruff`) sạch.

## 1d. T-017 — đã làm (2026-08-20)
**i18n (web):** Việt hóa toàn bộ nhãn/sai sót còn tiếng Anh — `profile.html`
(Unfriend→"Hủy kết bạn", Block User→"Chặn người dùng", Unblock→"Mở chặn",
Send Friend Request, Relationship→"Kết nối", các thông báo trạng thái),
"Threads/Thread"→"Bài viết/Viết bài/Đăng bài" (base nav, thread_new, feed
empty-state, activity), "Friends"→"Bạn bè" (people), "Like/follow"→
"Thích/theo dõi" (_right_rail), "Comment #"→"Bình luận #" (mod_reports) +
11 message validate trong `ting_ting/web/routes.py` (register/login/
profile/avatar/gender/friend-request/moderator).

**A11y:** nav icon-only (≤1100px) trước đó **mất accessible name** → thêm
`aria-label` cho mọi nav link + logout; `aria-current="page"` cho nav +
tabs feed/mod/activity; theme toggle `aria-pressed` (JS sync); badge
unread với ngữ cảnh sr-only; label↔input cho 2 ô ghi chú mod; aria-label
nút comment mang số lượng. Lighthouse a11y snapshot (login) = 100.

**2 bug thật tìm ra & sửa trong quá trình E2E:**
- CSS: `.composer-preview { display:flex }` ghi đè thuộc tính `hidden`
  → preview không ẩn sau khi gỡ ảnh. Sửa: `.composer-preview[hidden]{display:none}`
  (+ bump `?v=nodi-13`).
- Upload XHR: server reject trả 303 → XHR theo redirect, **mất lý do lỗi**
  (user thấy im lặng). Sửa: `create_post` trả JSON 422
  `{"error":{code,message VN}}` khi header `X-File-Upload: xhr`; composer
  hiện message server trong state div.

**Browser E2E (mới):** `docs/upload_e2e.py` (Playwright Chromium, chạy trên
dev server live, đăng ký user bỏ đi `e2e_up_*`): 20/20 checks desktop+
mobile — register→feed, preview+alt+gỡ, XHR upload render media, ZIP nhét
đuôi .png bị chặn **có message trên UI**, >25MB chặn client. Screenshots:
`docs/screenshots-e2e/`.

**Test:** cập nhật assertion theo string mới (test_web.py: 6 chỗ,
test_notifications.py: 1 chỗ). Full suite: **392 integration + 178 unit =
570 passed / 0 failed**, ruff sạch. Note: E2E tạo vài user `e2e_up_*` +
post test nhỏ trong dev DB (chủ đích, DB dev).

**Còn open:** screen-reader audit thật với NVDA/VoiceOver (chưa có tool
trên máy — audit tĩnh + Lighthouse + E2E Chromium đã phủ phần lớn).

## 1e. DB cleanup + T-018 phần local — đã làm (2026-08-20)
**Dọn dev DB (user yêu cầu):**
- Xóa 6 user `e2e_up_*` (id 37–42) + cascade trong 1 transaction
  (`PRAGMA foreign_keys=ON`): -5 posts, -5 post_media, -7 sessions (cascades
  comments/sessions). DB về đúng snapshot T-014: 36 users / 18 posts /
  2 post_media, 0 orphan (đã verify bằng query).
- Xóa 5 file media orphan `uploads/post-{38..42}-*.png`. E2E không tạo
  avatar. Dev service không bị đụng (transaction ngắn).

**T-018 phần local (pip-audit + CI audit job + git prep):**
- `pyproject.toml`: thêm `pip-audit>=2.7,<3.0` vào `[project.optional-
  dependencies].test`.
- `requirements-dev.lock`: re-resolve sạch venv (3.14.4 / pip 26.2.1):
  10 → **33 pins** (đủ dependency tree pip-audit 2.10.1).
- **Audit thật** (`pip-audit --disable-pip --no-deps`, PyPI+OSV):
  - `requirements.lock`: **1 CVE — ecdsa 0.19.2 (PYSEC-2026-1325 /
    CVE-2024-23342, Minerva timing attack P-256, CVSS 7.4, KHÔNG có phiên
    bản fix)**. Transitive của `python-jose[cryptography]`; app chỉ dùng
    **HS256 JWT** (`config.py jwt_algorithm`, `auth.py`) → code path ECDSA
    không chạy. **Waive có lý do** trong CI + mục 7.
  - `requirements-dev.lock`: **1 CVE — pytest 8.4.2 (PYSEC-2026-1845 /
    CVE-2025-71176, tmpdir handling, local vector, dev/CI-only; fix ở
    pytest 9.0.3 — vượt constraint `>=7.4,<9.0`)**. Waive, upgrade pytest 9
    track riêng nếu muốn.
- `.github/workflows/ci.yml`: job **`audit`** mới — cài dev lock, chạy
  `pip-audit -r <lock> --disable-pip --no-deps --ignore-vuln <id>` cho cả 2
  lock (audit đúng pins đã khóa, không re-resolve; 2 CVE waive bằng
  `--ignore-vuln` + comment lý do).
- `.gitignore`: thêm `*.sqlite`, `backups/`, `.tmpdbg/` (trước đó
  `backups/*.sqlite` + `.tar.gz` + drill artifacts chưa bị ignore).
- `pip-audit==2.10.1` đã cài vào `.venv` (chạy local giống CI).
- **CHƯA làm (cần human):** `git init` + initial commit + push lên GitHub
  remote (policy git-flow-ai deny Git write cho agent). Sau đó CI chạy thật
  = xác nhận lần đầu 2 lock install trên Python 3.13 (rủi ro còn ở mục 6).

## 1c. T-014 — đã làm (2026-08-20)
- Backup mới trên dev DB đang chạy (revision 0007): `scripts/backup.sh` →
  `backups/backup-20260820-101306.sqlite` + `backups/uploads-20260820-101307.tar.gz`.
- Restore drill vào location **disposable** `.tmpdbg/drill-0007/` (không đụng
  DB/dev service — service `active`, `/ready` 200 sau drill):
  copy snapshot + extract tar, verify bằng `.tmpdbg/drill-0007/verify.py`:
  - `integrity_check: ok`; `foreign_key_check`: 0 orphan.
  - `alembic_version = 20260819_0007`.
  - Counts: 36 users, 3 user_profiles, 4 friend_requests, 18 posts, 6 likes,
    7 comments, 12 follows, 23 activities, 2 saved_posts, 2 post_media,
    5 sessions (0 blocks/mutes/reports/reposts).
  - `posts -> users ON DELETE CASCADE` (0007 đúng).
  - Media: 2/2 file `post_media` có trong tar (basename sau `/uploads/`
    hay `/media/`); tar 120 files = `uploads/` live tại thời điểm backup.
- Evidence chi tiết cập nhật trong `PROJECT_STATUS_AND_ROADMAP.md` mục P2.3.

## 1b. T-013 — đã làm (2026-08-20)
- `docs/REPORT_2026-08-19_increments-5-to-10.md`: sửa 3 số sai (đã đối chiếu
  source: `grep -c def test_` → 19+19=38 moderation; 8 `@router` trong
  `ting_ting/api/moderation.py`): **22→38** test moderation, **12→8**
  endpoints moderation, delta **430→568 (+138) → 451→568 (+117)**. Thêm
  cập nhật T-012 vào known-gap CI.
- `PROJECT_STATUS_AND_ROADMAP.md` (authority): header + baseline **570**
  (568 + 2 test T-011); mục Giai đoạn 2 ghi T-011 (audit/rewrite 0007:
  orphan fail-closed, transactional_ddl, downgrade giữ check); P1.8 + P2.2
  ghi dual lock (T-012); P2.3 rõ "restore verified **tại 0006**, drill 0007
  pending (T-014)".
- `TING_TING_MVP_SCOPE.md` + `.biexce/MASTER_PLAN.md`: thêm banner
  **SUPERSEDED** (giữ nguyên nội dung, không xóa).
- `README.md`: install từ lockfiles; quota env (`TING_UPLOAD_QUOTA_MB` /
  `TING_TOTAL_UPLOAD_QUOTA_MB`); section **API** (`/api/v1` canonical +
  `/api` legacy header, session lifecycle refresh/logout/logout-all/
  change-password, media authorized + quota, `/health` `/ready`
  `/metrics` + `X-Request-ID`); Architecture thêm modules
  (uploads/moderation/sessions/notifications/keyset/metrics).
- `docs/RUNBOOK.md`: §1 install từ lock; §2 rollback migration
  **0002–0007** (từng migration 1 DB transaction; downgrade 0007 fail-closed
  khi orphan); §6 thêm 5 env vars; §7 cutover PG: 0002–0007 + dùng
  `ting_ting.migrate_data` thay `sqlite3 .dump` thủ công.
- Verify: `ruff check` sạch; grep confirm các số đã sửa; chưa cần test (docs-only).

## 1a. T-012 — đã làm (2026-08-19/20)
- `requirements.lock` (viết lại): **38 pins runtime** — resolve sạch từ
  `pip install .` trên venv trống (Python 3.14.4, pip 26.2.1). Bỏ dòng
  `-e /home/chihai/...` (old line 50) và các package dev/stray (pytest,
  httpx, playwright, passlib, Pygments...). Giữ `python-dotenv`/`PyYAML`
  (là transitive hợp lệ: pydantic-settings / uvicorn[standard]).
- `requirements-dev.lock` (mới): **10 pins** dev/test-only cài TRÊN lock prod
  (pytest, httpx, pytest-asyncio, ruff==0.16.3 + transitive). Ghi chú rõ
  playwright (chỉ `docs/smoke_browser*.py`) **không** pin ở đây.
- `.github/workflows/ci.yml`: lint job cài `requirements-dev.lock` (ruff pin
  một nơi); test job cài `requirements.lock` + `requirements-dev.lock` +
  `pip install --no-deps -e .` (không re-resolve, lock giữ nguyên giá trị).
- Proof venv sạch (3.14.4): cài 2 lock + `--no-deps -e .` → **thành công**;
  `ruff check` **sạch**; `pytest tests/unit tests/integration/test_database_
  migration.py` → **183 passed** (targeted, chưa chạy full suite).

## 2. Đã hoàn thành (T-011, trước đó)
- Viết lại `alembic/versions/20260819_0007_posts_author_cascade.py`:
  - Orphan preflight fail-closed (`ValueError`) **trước** khi chạm DDL.
  - Rebuild bảng `posts` cho cả upgrade lẫn downgrade dùng **một**
    `_rebuild_table()` (khóa `ck_post_audience` + column types không bị drift).
  - Downgrade giờ giữ đúng check constraint (bản cũ bị mất `ck_post_audience`).
- `alembic/env.py`: bật `transactional_ddl=True` + `transaction_per_migration=True`
  để Alembic tự bọc mỗi migration trong **một transaction thật** -> rebuild
  atomic; version stamp commit cùng transaction.
- `tests/integration/test_database_migration.py`: thêm 2 test:
  - `test_0007_roundtrip_preserves_post_children` (upgrade + downgrade giữ đủ
    child rows của mọi bảng phụ thuộc posts; FK CASCADE bật/tắt đúng).
  - `test_0007_upgrade_fails_closed_on_orphan_posts` (orphan -> raise; revision
    và data không đổi, transaction roll back).
- Verification data-loss: so `.tmpdbg/ting_ting.db.bak-pre0007` (pre-0007,
  stamped 0006) với `ting_ting.db` -> **không mất dữ liệu**. Chênh lệch chỉ do
  hoạt động hợp lệ sau backup + cascade của user `uitest` (id 37) bị xóa chủ
  đích (chứng minh cascade đúng).

## 3. Quyết định kỹ thuật quan trọng
- **Dùng `transactional_ddl=True` + `transaction_per_migration=True` trong
  env.py**, KHÔNG tự `connection.begin()/commit()` trong migration. Lý do: bản
  đầu dùng manual transaction đã phá **version stamp** của Alembic (schema có
  CASCADE nhưng `alembic_version` vẫn 0006) -> regression nghiêm trọng. Để
  Alembic tự quản transaction là cách đúng và ít rủi ro.
- **Lưu ý tác động toàn cục:** thay đổi env.py ảnh hưởng **mọi** migration
  (0001-0007 và các migration tương lai) — mỗi file chạy trong một DB
  transaction (atomic theo file, không atomic xuyên file). An toàn trên SQLite.
- `op.execute(<SELECT>)` trả về `None` (không có `.scalar()`) -> đọc bằng
  `op.get_bind().execute(...).scalar()`.
- **Không dùng `sa.exc.MigrationException`** — không tồn tại ở SA 2.0.52 /
  alembic 1.19.1. Dùng `ValueError` (thống nhất với `ting_ting.migrate_data`).
- **Gotcha SQLAlchemy 2.0 multi-row insert:** `conn.execute(T.__table__.insert(),
  [d1, d2])` compile column theo **dict đầu tiên**; key chỉ có ở dict sau sẽ bị
  bỏ (gặp ở `parent_comment_id`). Sửa bằng cách **keys đồng nhất** giữa các row.
- `PRAGMA foreign_key_list`: `r[5]=on_update`, `r[6]=on_delete` (dùng r[6]).
- **T-012 lock strategy:** lock = **version-only** (không path/editable).
  `requirements.lock` = chỉ runtime; `requirements-dev.lock` = dev-only cài
  thêm. CI install package bằng `pip install --no-deps -e .` (KHÔNG
  `pip install -e ".[test]"` — cái sau re-resolve và có thể phá tính
  reproducible của lock). Resolve trên 3.14 nhưng pins đều version chung,
  CI 3.13 chỉ cần có wheel tương ứng (chưa kiểm chứng local — không có
  python3.13 trên máy).
- Dev DB `ting_ting.db` đã ở 0007 (có CASCADE) — migration mới **chưa** chạy lại
  trên nó; bản viết lại là để các DB chạy mới/rollback-analyze. Backup pre-0007
  nằm ở `.tmpdbg/ting_ting.db.bak-pre0007`.

## 4. File đã sửa và lý do
**T-018 local + DB cleanup:**
- `.gitignore` — thêm `*.sqlite`, `backups/`, `.tmpdbg/` (bảo vệ data/dev
  artifacts khỏi bị commit khi init git).
- `pyproject.toml` — `pip-audit>=2.7,<3.0` vào test extras (nguồn của dev lock).
- `requirements-dev.lock` — re-resolve: 10 → 33 pins (thêm tree pip-audit).
- `.github/workflows/ci.yml` — job `audit` (pip-audit 2 lock, waive 2 CVE).
- `ting_ting.db` (dev, KHÔNG phải source) — xóa 6 user `e2e_up_*` + cascade;
  `uploads/` — xóa 5 file `post-{38..42}-*.png` orphan.

**T-017 (+ fix backup.sh):**
- `scripts/backup.sh` — nhận diện URL SQLAlchemy `postgresql+psycopg://` (trước
  đó chỉ `postgresql://`/`postgres://` → fall through sang nhánh SQLite); chuyển
  về `postgresql://` cho `pg_dump`. Test bằng stub: 3 case OK.
- `ting_ting/web/routes.py` — Việt hóa 11 message validate; `create_post` trả
  JSON 422 khi upload XHR bị `UploadRejected` (header `X-File-Upload: xhr`).
- Templates (`base`, `feed`, `profile`, `thread_new`, `people`, `activity`,
  `mod_reports`, `_right_rail`) — i18n + a11y (aria-label/aria-current/
  aria-pressed, label↔input, sr-only badge).
- `ting_ting/static/style.css` — `.composer-preview[hidden]{display:none}`.
- `docs/upload_e2e.py` (mới) + `docs/screenshots-e2e/` — browser E2E upload.
- `tests/integration/test_web.py` (6 assertion), `tests/integration/
  test_notifications.py` (1 assertion) — cập nhật theo string UI mới.

**T-012:**
- `requirements.lock` — viết lại: 38 pins runtime, không editable/absolute path,
  không dev package.
- `requirements-dev.lock` — mới: 10 pins dev/test (kèm header giải thích).
- `.github/workflows/ci.yml` — install từ 2 lock + `--no-deps -e .`; ruff pin
  trong dev lock (không hardcode nữa).

**T-011 (trước đó):**
- `alembic/env.py` — bật transactional DDL + transaction per migration (atomic,
  version stamp correct).
- `alembic/versions/20260819_0007_posts_author_cascade.py` — rewrite: orphan
  preflight, rebuild atomic, share schema upgrade/downgrade, giữ check constraint.
- `tests/integration/test_database_migration.py` — thêm helpers
  (`_run`, `_seed_post_graph`, `_child_counts`, `_post_fk_state`) + 2 test mới;
  sửa seed comment (keys đồng nhất) và FK index (r[6]).

## 5. Test / lint đã chạy + kết quả
**T-017 (sau khi sửa i18n/a11y/bug):**
- `ruff check ting_ting tests` -> **All checks passed!**
- `pytest tests/integration -q` -> **392 passed / 0 failed** (~4 phút; đã fix
  đúng 1 test assert string UI cũ).
- `pytest tests/unit -q` -> **178 passed / 0 failed**. Tổng = **570 passed**.
- `docs/upload_e2e.py` (Chromium thật, dev server live) -> **20/20 checks**
  desktop + mobile (evidence screenshots `docs/screenshots-e2e/`).
- `backup.sh` + stub `pg_dump`: 3 URL cases (SQLAlchemy driver, plain,
  SQLite fallback) -> đúng nhánh, URL chuyển đúng.
- Lighthouse accessibility snapshot (login) -> **100/100**.

**T-012 (proof trên venv sạch 3.14.4, cài 2 lock + `--no-deps -e .`):**
- Install 2 lock + package -> **thành công** (không lỗi resolve).
- `ruff check ting_ting tests` -> **All checks passed!**
- `pytest tests/unit tests/integration/test_database_migration.py -q` ->
  **183 passed, 1 warning in 60.47s** (targeted; full suite chưa chạy lại).
- YAML `ci.yml` parse OK; 2 lock không còn line absolute/editable (đã kiểm
  bằng script: 0 bad lines).

**T-011:**
- `.venv/bin/ruff check ting_ting tests` -> **All checks passed!**
- `.venv/bin/python -m pytest tests/integration/test_database_migration.py -v`
  -> **5 passed** (kể cả 2 test mới).
- Full suite (chạy 1 lần, trước khi user yêu cầu dừng): `.venv/bin/python -m
  pytest -q` -> **570 passed, 15 warnings in 291.81s**.

## 6. Lỗi / rủi ro còn mở
- **env.py là thay đổi toàn cục** trên đường migration. Green trên SQLite
  (full suite pass) nhưng **PG chưa kiểm** (T-015). Cần xác nhận 0001-0007 vẫn
  chạy đúng trên PostgreSQL sau khi bật transactional DDL.
- Downgrade preflight **cố ý** raise khi có orphan (sau khi cascade, downgrade
  về schema legacy mà giữ orphan là bất nhất). Vận hành cần biết điều này.
- Dữ liệu dev vẫn là SQLite production (0007); cutover PG vẫn chưa (T-015/16).
- **T-012 lock resolve trên Python 3.14** nhưng CI chạy **3.13** — chưa có
  python3.13 local để kiểm chứng wheel/cài đặt; lần chạy CI đầu trên GitHub
  mới xác nhận final. (Rủi ro thấp: các pin đều là release mới hỗ trợ 3.13.)
- `docs/smoke_browser*.py` (playwright) không nằm trong dev lock — ai chạy
  kịch bản đó phải `pip install playwright` + `playwright install chromium`
  thủ công trên dev env (`.venv` hiện tại đã có sẵn).
- Các task production khác vẫn chưa làm (xem mục 7).

## 7. Công việc còn lại
1. **T-015 (P1, PENDING — user: vẫn chạy SQLite, chờ thông tin PG) — PG
   migration spike:** 0001→head, downgrade/upgrade, copy SQLite, count/
   checksum, reset sequence, lock timing, query plan. (Bao gồm xác nhận
   env.py transactional_ddl an toàn trên PG + drill backup/restore bằng
   `pg_dump`/`pg_restore`.)
2. **T-016 (P1, PENDING — sau T-015 + credential) — PG production cutover.**
3. **T-018 (P2 — phần local XONG, còn phần remote) — HUMAN làm:**
   `git init` + initial commit (`.gitignore` đã phủ `.env`, `*.db`,
   `*.sqlite`, `uploads/`, `backups/`, `.tmpdbg/`, `.venv/` — đã audit) +
   push lên Git remote + bật CI. Lần chạy CI đầu sẽ xác nhận lần đầu lock
   install trên Python 3.13 + job `audit`.
4. **Waived CVE (ghi nhận có chủ đích, xem §1e):** ecdsa PYSEC-2026-1325,
   pytest PYSEC-2026-1845. CI job `audit` chặn CVE mới khác 2 id này.
5. **Không block nhưng chưa làm (P3, tùy chọn):** screen-reader audit thật
   với NVDA/VoiceOver. (Dọn user `e2e_up_*` + media orphan trong dev DB
   — **đã xong 2026-08-20**, xem §1e.)

## 8. Lệnh test ngắn cần chạy tiếp
```bash
# Lint nhanh
.venv/bin/ruff check ting_ting tests

# Focused (chạy lại trước tiên khi đụng migration/env)
.venv/bin/python -m pytest tests/integration/test_database_migration.py -v

# Full gate (chỉ khi cần nghiệm thu cuối)
.venv/bin/python -m pytest -q
```

Lưu ý: khi sửa `alembic/env.py` hoặc migration, luôn chạy lại test migration
focused trước; nếu đổi behavior chung thì mới chạy full suite. Không chạy full
test suite theo yêu cầu user cho đến khi được phép.