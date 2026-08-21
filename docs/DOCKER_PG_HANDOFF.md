# Docker + PostgreSQL — Tổng hợp quyền & lệnh chạy (bàn giao IT)

Mục tiêu: triển khai app + PostgreSQL **chỉ bằng container** (IT xác nhận
2026-08-21: "chạy docker thôi"), **giữ và migrate toàn bộ dữ liệu SQLite hiện
hành** (quyết định sản phẩm 2026-08-21). Tài liệu này là checklist duy nhất:
phần nào IT cần phê duyệt/cấp quyền, phần nào bên dev đã làm, lệnh chạy từng
bước kèm kết quả mong đợi để IT gửi lại làm evidence.

Trạng thái repo tại thời điểm bàn giao:

- Migration chain `0001→0011` chạy sạch trên **cả hai** dialect; nhánh PG
  đã sửa đầy đủ bằng `20260821_0011_postgres_parity` (mute check constraint,
  `hidden_posts`, default `reports.status='pending'`, re-anchor sequences).
- App **từ chối khởi động** nếu PostgreSQL không alembic-stamp, hoặc stamp
  ≠ head; DB PG rỗng được app tự migrate khi start.
- `Dockerfile` (non-root, Python 3.13), `compose.yaml` (db / migration /
  data-copy / app), `.dockerignore` đã có trong repo.
- `tests/integration/test_pg_cutover.py` là **gate PG**: migrations thật,
  4 sửa 0011, copy roundtrip + sequence, API smoke — CI job `postgres`
  chạy tự động trên `postgres:16`; máy dev hiện **chưa có quyền
  docker.sock** nên gate này xác nhận qua CI (hoặc IT chạy local).
- Dev DB (SQLite) đang ở `0011` (head), service `:8080` ready.

---

## 1. IT cần quyết định / cung cấp (6 mục)

Gửi kèm 6 câu hỏi này cho IT; nếu đồng ý đề xuất thì chỉ cần trả lời
"đồng ý" + tên server/registry.

| # | Việc cần IT | Đề xuất của dev |
|---|---|---|
| 1 | Ảnh PostgreSQL được duyệt | `postgres:16` (đã dùng trong compose + CI; psycopg3 hỗ trợ đầy đủ) |
| 2 | Base image Python / registry nội bộ (nếu có) | `python:3.13-slim` (bằng CI). Dockerfile có `ARG PYTHON_BASE` — IT override registry nội bộ bằng `docker build --build-arg PYTHON_BASE=<registry>/python:3.13-slim` |
| 3 | Cách IT inject secrets (`TING_JWT_SECRET`, `POSTGRES_PASSWORD`) | Env file riêng trên host deploy, ngoài repo. Compose bắt buộc 2 biến này (`:?` — thiếu là từ chối chạy) |
| 4 | Lõi lưu trữ cho PG data + uploads | Named volume của Docker (`pgdata`, `uploads-data`) — đơn giản, cùng host. Nếu cần backup về NAS → IT nêu, dev chỉnh |
| 5 | Ai quản lý ingress/TLS/port công khai → `:8080`? | IT (nginx/Traefik có sẵn). `app` bind `127.0.0.1:8080` trên host; `db` **không publish port** (chỉ mạng nội bộ compose) |
| 6 | Maintenance window ngừng ghi SQLite để copy | 30–60 phút, khung giờ IT chọn (dữ liệu hiện ~vài MB, copy nhanh; margin cho smoke) |

## 2. Quyền cần xin (chỉ 2 mục — 1 mục có điều kiện)

| # | Quyền | Ai cần | Khi nào |
|---|---|---|---|
| Q1 | **Docker daemon access** cho user `chihai` (docker group/Docker context do IT quản lý) — **CHỈ CẦN nếu dev được tự build/chạy để diễn tập**. Nếu IT tự chạy hết → **không cần xin** | dev | trước rehearsal |
| Q2 | **SSH/deploy access** tới server đích (nếu chưa có): tạo thư mục deploy, đặt `compose.yaml` + secrets, mount file SQLite cũ, đọc được `127.0.0.1:8080` | dev hoặc IT | trước rehearsal |

Không cần xin: quyền admin PostgreSQL trên host, systemd, nginx — tất cả
nằm trong container hoặc IT quản lý. Không thêm user vào docker group nếu
IT là người chạy (quyền tương đương root, không cần).

Trạng thái máy dev hiện tại: docker CLI `29.6.2` + Compose `v5.3.1` đã cài,
nhưng `/var/run/docker.sock` → **permission denied** (user không thuộc
group `docker`). Việc viết/validate config vẫn làm được
(`docker compose config` không cần daemon — đã validate xanh).

## 3. Dev đã tự làm trước bàn giao (đang nằm trong repo)

1. ✅ `Dockerfile` — non-root (uid 10001), `requirements.lock` (pinned) rồi
   `pip install --no-deps -e .` (editable để embedded alembic tìm đúng
   `alembic.ini`), runtime uvicorn **1 worker** (rate-limit/metrics/
   upload-quota là trạng thái in-process), HEALTHCHECK `/health`.
2. ✅ `compose.yaml` — `db` (postgres:16, volume `pgdata`, healthcheck
   `pg_isready`, không publish port); `migration` (one-shot
   `alembic upgrade head`, chờ db healthy); `data-copy` (profile `data`,
   mount SQLite **read-only**, chạy `ting_ting.migrate_data`); `app`
   (volume `uploads-data` → `/app/uploads`, `127.0.0.1:8080`, chờ migration
   completed successfully).
3. ✅ `.dockerignore` — loại `.env`, `*.db*`, `uploads/`, `backups/`,
   `.venv/`, `tests/`, VCS, egg-info...
4. ✅ Code fixes: `TING_UPLOADS_DIR` (mặc định dev không đổi), PG startup
   fail-closed + head check, migration `0011` (parity + sequences),
   `migrate_data` hardened, package-data (templates/static vào wheel),
   fix `_with_database_url` (bỏ mask password — trước đây làm hỏng mọi
   PG connection embedded).
5. ✅ Test + docs: `test_pg_cutover.py` (6 test, CI job `postgres`),
   `docker compose config` xanh (CI validate + local verify), README/
   RUNBOOK/`.env.example` cập nhật.

Local verify đã chạy (không cần Docker):

```bash
.venv/bin/ruff check ting_ting tests alembic            # xanh
.venv/bin/python -m pytest -q                            # 647 passed / 7 skipped
.venv/bin/pip wheel --no-deps -w /tmp/wheel .            # wheel chứa 12 templates + static
POSTGRES_PASSWORD=x TING_JWT_SECRET=x \
  docker compose config --quiet                          # xanh (không cần daemon)
```

## 4. Playbook cho IT — lệnh chạy

Ký hiệu: `<DEPLOY_DIR>` = thư mục repo trên server; file SQLite cũ
(`ting_ting.db`) và thư mục `uploads/` cũ nằm ở đâu đó trên host — IT chỉ
cần biết đường dẫn (truyền qua `SQLITE_SOURCE`). Mỗi bước ghi **MONG ĐỢI** —
IT gửi lại output thực tế làm evidence.

### Bước A — Dựng (không đụng production)

```bash
cd <DEPLOY_DIR>
git pull                                   # code bàn giao (dev đã push)
# IT đặt secrets vào env của máy (compose.yaml khai báo yêu cầu 2 biến):
#   POSTGRES_PASSWORD, TING_JWT_SECRET   (openssl rand -hex 16 / -hex 32)
docker compose config --quiet              # MONG ĐỢI: không lỗi (exit 0)
docker compose build                       # MONG ĐỢI: build xong
```

Evidence bước A (IT chạy 1 lệnh, gửi output — kiểm chứng image sạch):

```bash
docker run --rm --entrypoint python biexce-social:local -c "
import pathlib
root = pathlib.Path('/app')
assert not (root / '.env').exists(), '.env leaked'
assert not list(root.glob('*.db*')), 'DB leaked'
assert not (root / 'backups').exists(), 'backups leaked'
p = pathlib.Path(__import__('ting_ting').__file__).parent
assert (p / 'web' / 'templates').is_dir() and (p / 'static').is_dir()
print('image hygiene OK')"
```

### Bước B — Dừng writer + backup (maintenance window, mục 1.6)

```bash
systemctl --user stop biexce-social-user.service     # stop app SQLite
cd <REPO_DIR_CŨ> && scripts/backup.sh                # snapshot DB + tar uploads/
ls -la backups/ | tail -3                            # MONG ĐỢI: 2 file mới
```

### Bước C — Database container + migration

```bash
cd <DEPLOY_DIR>
docker compose up -d db                              # MONG ĐỢI: db "healthy"
docker compose exec db pg_isready                    # MONG ĐỢI: "accepting connections"
docker compose run --rm migration                    # MONG ĐỢI: alembic 0001..0011, exit 0
docker compose run --rm migration alembic current    # MONG ĐỢI: "20260821_0011 (head)"
docker compose exec db psql -U biexce_social -d biexce_social \
  -c "select count(*) from users"                    # MONG ĐỢI: 0
```

### Bước D — Copy dữ liệu SQLite → PostgreSQL (one-shot, fail-closed)

```bash
SQLITE_SOURCE=<path tới ting_ting.db> \
  docker compose --profile data run --rm data-copy
# MONG ĐỢI: dòng "Migration completed and verified." + count từng bảng, exit 0
# (Tool tự từ chối nếu: source không ở revision head, source thiếu cột,
#  target không rỗng, hoặc count/max(id) sau copy không khớp — và KHÔNG
#  ghi gì nửa chừng vì copy chạy trong 1 transaction.)
docker compose exec db psql -U biexce_social -d biexce_social -c "
select (select count(*) from users) users,
       (select count(*) from posts) posts,
       (select count(*) from comments) comments,
       (select count(*) from reports) reports;"
# MONG ĐỢI: khớp count của các bảng in ra ở bước trên
# Chạy lại lần nữa để xác nhận fail-closed:
# MONG ĐỢI: "ERROR: migration aborted: Target database is not empty"
SQLITE_SOURCE=<path tới ting_ting.db> \
  docker compose --profile data run --rm data-copy
```

### Bước E — Copy uploads

```bash
# uploads-data là named volume của compose project "biexce-social":
docker cp <REPO_DIR_CŨ>/uploads/. biexce-social-app-1:/app/uploads
# (tên container: docker compose ps -q app → docker inspect --format '{{.Name}}' $(docker compose ps -q app) | tr -d /)
# SO SÁNH số file 2 phía:
find <REPO_DIR_CŨ>/uploads -type f | wc -l
docker compose exec app find /app/uploads -type f | wc -l
# MONG ĐỢI: bằng nhau (trừ file đã bị cleanup giữa 2 thời điểm — đối chiếu tên nếu khác)
```

### Bước F — Chạy app trên PostgreSQL + smoke

```bash
docker compose up -d app                   # MONG ĐỢI: app "healthy"/"running"
docker compose ps                          # db healthy, app running/healthy
curl -s http://127.0.0.1:8080/health       # MONG ĐỢI: {"status":"ok"}
curl -s http://127.0.0.1:8080/ready        # MONG ĐỢI: {"status":"ready","database":"ok"}
docker compose logs app --tail 50          # MONG ĐỢI: không lỗi exception
```

Smoke nghiệp vụ (dev chạy hoặc IT hỗ trợ, tài khoản thật):

```bash
# 1. Đăng nhập 1 user tồn tại từ SQLite (mật khẩu KHÔNG đổi khi copy)
# 2. Xem feed, đọc 1 post cũ
# 3. Tạo 1 post thử có ảnh (chứng minh /app/uploads volume hoạt động)
# 4. Trả lời comment trên post cũ (chứng minh FK + sequence sau copy)
# MONG ĐỢI: không 5xx; log app không exception
```

### Bước G — Chốt cửa sổ + backup PG

```bash
docker compose exec db pg_dump -Fc -U biexce_social -d biexce_social \
  -f /tmp/after-migration.dump
docker cp db:/tmp/after-migration.dump <DEPLOY_DIR>/backups/
# Restart để chứng nhận volume bền (data + uploads không mất):
docker compose restart app && sleep 5 && curl -s http://127.0.0.1:8080/ready
# MONG ĐỢI: {"status":"ready","database":"ok"}
# KHÔNG bật lại service SQLite cũ. Giữ file ting_ting.db + uploads cũ
# NGUYÊN (rollback target) 7 ngày kể từ hôm chuyển.
```

Bắt đầu cron backup PG thay cron SQLite (mục 3 RUNBOOK):
`docker compose exec db pg_dump -Fc -U biexce_social -d biexce_social -f /tmp/biexce-$(date +%F).dump`
rồi `docker cp` ra `backups/`.

## 5. Evidence IT gửi lại dev (để đặt trạng thái Done)

1. Output `docker compose config` + build (bước A) + "image hygiene OK".
2. `pg_isready` + output `alembic current` (bước C).
3. Count từng bảng sau copy + count SQLite trước copy (bước D) — **khớp**,
   và output "target not empty" khi chạy lại.
4. Số file uploads 2 phía (bước E).
5. `/health`, `/ready`, `docker compose logs app --tail 50` (bước F).
6. Kết quả smoke nghiệp vụ (screenshot hoặc log).
7. File `after-migration.dump` tồn tại + `/ready` sau restart (bước G).

## 6. Rollback (trong cửa sổ 7 ngày đầu)

1. `docker compose stop app`.
2. Giữ nguyên file SQLite cũ (không migrate ngược — SQLite là snapshot tại
   thời điểm copy).
3. Bật lại `biexce-social-user.service` (`.env` vẫn trỏ SQLite).
4. `curl /health` + `/ready` → thông báo user: **dữ liệu tạo mới từ thời
   điểm copy sẽ mất** (đã cảnh báo trước khi đóng write).
5. Muốn cứu data mới trên PG: `after-migration.dump` (bước G) hoặc giữ
   volume `pgdata` không xóa.

Sau 7 ngày ổn định: disable service SQLite cũ; giữ file SQLite làm archive;
xác định cron backup PG là chuẩn.

## 7. Acceptance (đạt tất cả mới tính "chuyển xong")

- [x] Migrations `0001→0011` áp dụng trên PG thật (CI job `postgres`).
- [x] App từ chối khởi động khi PG không stamp / stamp ≠ head (test gate).
- [x] `docker compose config` + build xanh; image không chứa file cấm (CI).
- [ ] Image chạy non-root, PG không publish port (IT confirm bước A/C).
- [ ] Copy toàn bộ SQLite + uploads, count khớp — **giữ dữ liệu cũ**
      (quyết định 2026-08-21) (bước D/E).
- [ ] Insert dữ liệu mới sau import không lỗi sequence (bước F, test gate
      đã chứng minh cơ chế; IT smoke xác nhận thực tế).
- [ ] `/health`, `/ready` 200; smoke nghiệp vụ xanh (bước F).
- [ ] `pg_dump` + restart container → data + uploads còn (bước G).
- [ ] IT xác nhận cron backup/restore định kỳ chạy được (bước G + RUNBOOK §3).