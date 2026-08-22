# Biexce Social — Runbook

Thủ tục vận hành: deploy, rollback, backup/restore, sự cố. Môi trường hiện tại:
service systemd **user** `biexce-social-user.service` trên `:8080`, SQLite
`ting_ting.db`. Cutover PostgreSQL sẽ chạy theo mô hình **Docker-only** (IT
xác nhận 2026-08-21) — playbook đầy đủ: `docs/DOCKER_PG_HANDOFF.md`.

## 1. Deploy code mới

```bash
cd ~/workspace/biexce-social-backend-slim   # repo
.venv/bin/python -m pytest -q                # 1. kiểm chứng xanh (full suite ~5 phút)
.venv/bin/ruff check ting_ting tests         # 2. lint xanh
# 3. migration nếu có revision mới:
TING_DATABASE_URL="$(grep TING_DATABASE_URL .env | cut -d= -f2-)" \
  .venv/bin/python -m alembic upgrade head
# 4. restart:
systemctl --user restart biexce-social-user.service
systemctl --user is-active biexce-social-user.service    # -> "active"
curl -s http://127.0.0.1:8080/health                     # -> {"status":"ok"}
curl -s http://127.0.0.1:8080/ready                       # -> {"status":"ready",...}
```

Lưu ý: chạy alembic **luôn** qua Python subprocess với `TING_DATABASE_URL`
truyền trực tiếp (alembic CLI trong shell bị permission-block; và nếu quên
biến môi trường, `alembic.ini` mặc định trỏ DB trong repo — chính xác dev,
nhưng phải chủ động). Môi trường Python phục hồi từ lock:
`pip install -r requirements.lock -r requirements-dev.lock && pip install --no-deps -e .`.

## 2. Rollback

- **Code**: quay về commit trước (sau khi `git revert`/checkout), rồi
  `systemctl --user restart biexce-social-user.service`.
- **Data (chỉ khi migration mới gây hỏng)**:
  `alembic downgrade -1` rồi restart. Migration SQLite 0002–0007 có downgrade
  đã viết (từng migration chạy trong 1 DB transaction — atomic); 0003 chỉ an
  toàn trên DB chưa có dữ liệu (có comment trong file). Downgrade 0007
  **fail-closed**: nếu tồn tại orphan (post trỏ user đã mất) nó raise thay vì
  ghi — bình thường không có orphan vì cascade. Trước mọi migration nặng:
  chạy `scripts/backup.sh` và ghi nhận `alembic_version` hiện tại.

## 3. Backup / Restore

```bash
scripts/backup.sh                 # snapshot DB nhất quán (online) + tar uploads/
ls backups/                       # backup-<ts>.sqlite + uploads-<ts>.tar.gz
# giữ 10 bản mới nhất mỗi loại, bản cũ tự xóa.
```

- **Tự động hàng ngày** (cron user, không cần sudo):
  `crontab -e` → `0 3 * * * $HOME/workspace/biexce-social-backend-slim/scripts/backup.sh >> $HOME/workspace/biexce-social-backend-slim/backups/cron.log 2>&1`
- **Restore**:
  `scripts/restore_sqlite.sh backups/backup-<ts>.sqlite`
  (script tự verify, tự sao an toàn DB hiện tại ra `backups/pre-restore-<ts>.sqlite`,
  stop → replace → start service. Restore uploads bằng `tar -xzf` trong repo).
- PostgreSQL (sau cutover, chạy trong container):
  `docker compose exec db pg_dump -Fc -f /tmp/biexce.dump biexce_social`
  rồi `docker cp db:/tmp/biexce.dump backups/` — cron hàng ngày tương đương
  cron SQLite ở trên. Restore: `docker compose exec -i db pg_restore -U
  biexce_social -d biexce_social --clean < biexce.dump` (dừng app trước).
  Uploads sống trong volume `uploads-data`; backup bằng
  `docker run --rm -v <project>_uploads-data:/u --entrypoint tar biexce-social:local -czf - -C /u .`

## 4. Quan sát

- `GET /health` — process sống (chưa kiểm DB).
- `GET /ready` — DB trả lời được; 503 khi DB hỏng.
- `GET /metrics` — Prometheus text: `http_requests_total{status_class}`,
  histogram latency ms, `auth_login_failures_total`.
- Access log: mỗi request 1 dòng `rid=<request_id> method path -> status duration_ms`
  (stdout của service → `journalctl --user -u biexce-social-user.service -f`).
- Mọi response có header `X-Request-ID` (gửi đi header thì được echo lại) —
  dùng để nối log khi điều tra.

**Cần xem xét ngay**: 5xx tăng đột biến; `auth_login_failures_total` tăng
mạnh (đang bị brute-force — rate limit login là 20/phút/IP, xem xét giảm);
`/ready` trả 503.

## 5. Sự cố thường gặp

| Triệu chứng | Chẩn đoán | Xử lý |
|---|---|---|
| Service dead | `systemctl --user status biexce-social-user.service` + `journalctl --user -u biexce-social-user.service -n 100` | Sửa lỗi, restart. Nếu crash do DB: xem `/ready`, restore backup nếu corrupt |
| DB corrupt / file mất | `PRAGMA integrity_check` trên file | Restore từ `backups/` mới nhất bằng `scripts/restore_sqlite.sh` |
| Đột ngột 500 sau deploy | `journalctl` + `X-Request-ID` | `alembic current` đối chiếu revision; rollback code hoặc migration (mục 2) |
| User báo bị khóa oan | `SELECT id,username,banned_at,banned_until,ban_reason FROM users WHERE banned_at IS NOT NULL AND (banned_until IS NULL OR banned_until > CURRENT_TIMESTAMP)` | Đối chiếu `moderation_actions`, rồi unban từ trang mod (`/web/mod/reports`) khi xác nhận nhầm lẫn |
| Disk đầy | `du -sh . uploads/ backups/ logs*` | Xóa backup cũ (tự giữ 10), dọn uploads orphan |
| Bị brute-force login | `/metrics` → `auth_login_failures_total` | Rate limit đang chặn 20 req/phút/IP; ghi nhận IP, cân nhắc ban via mod tools |

## 6. Biến môi trường (`.env`, không commit)

- `TING_DATABASE_URL` — `sqlite:///ting_ting.db` hoặc `postgresql+psycopg://...`
- `TING_JWT_SECRET` — secret dài ngẫu nhiên (bắt buộc)
- `TING_COOKIE_SECURE` — `true` khi chạy behind HTTPS
- `TING_RATE_LIMIT_ENABLED` — `true/false`
- `TING_SESSION_EXPIRE_DAYS` — mặc định 7 (tuổi session đăng nhập)
- `TING_JWT_EXPIRE_MINUTES` — mặc định 60 (JWT ngắn hạn, refresh qua session)
- `TING_DEMO_PASSWORD` — bắt buộc khi chạy `python -m ting_ting seed`
- `TING_UPLOAD_QUOTA_MB` — quota upload/user, mặc định 512
- `TING_TOTAL_UPLOAD_QUOTA_MB` — quota upload toàn hệ thống, mặc định 5120
- `TING_UPLOADS_DIR` — thư mục media; dev để `uploads` (repo), container
  trong compose bị set `/app/uploads` (volume `uploads-data`)

Biến riêng của môi trường host chạy compose (không nằm trong `.env` app):
`POSTGRES_PASSWORD`, `TING_JWT_SECRET` (compose.yaml khai báo bắt buộc),
tuỳ chọn `SQLITE_SOURCE`, `TING_COOKIE_SECURE`, `TING_RATE_LIMIT_ENABLED`,
`TING_DEBUG`.

## 7. Cutover PostgreSQL (mô hình Docker-only)

Playbook đầy đủ (trách nhiệm ai làm gì, lệnh từng bước, evidence):
**`docs/DOCKER_PG_HANDOFF.md`**. Tóm tắt:

1. Dựng DB + migration trong container: `docker compose up -d`
   (services `db` → `migration` → `app`; app tự từ chối chạy nếu
   alembic_version ≠ head).
2. Dừng writer SQLite trong maintenance window + `scripts/backup.sh`.
3. Copy dữ liệu (one-shot, fail-closed):
   `SQLITE_SOURCE=<path ting_ting.db> docker compose --profile data run --rm data-copy`
   — giữ ID, verify count + max(id) từng bảng, re-anchor sequence; từ chối
   nếu target không rỗng hoặc nguồn lệch schema/revision.
4. Copy `uploads/` vào volume `uploads-data`.
5. Smoke + `pg_dump` ngay sau đó (mục 3); giữ file SQLite cũ làm rollback
   target cho tới 7 ngày ổn định (rollback: mục 6 của handoff doc).

## 8. Cấp role kiểm duyệt (dev/operator)

```sql
UPDATE users SET role = 'moderator' WHERE username = '<user>';
-- Chỉ bootstrap admin qua operator access đã kiểm soát; ứng dụng không tự tạo admin.
UPDATE users SET role = 'admin' WHERE username = '<bootstrap-admin>';
```

Role được đọc trực tiếp từ DB mỗi request, không nằm trong JWT; session hiện tại
nhận quyền mới ngay. Ghi nhận bootstrap admin trong change ticket/operator audit.
