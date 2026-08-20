# Biexce Social — Runbook

Thủ tục vận hành: deploy, rollback, backup/restore, sự cố. Môi trường hiện tại:
service systemd **user** `biexce-social-user.service` trên `:8080`, SQLite
`ting_ting.db` (PostgreSQL là bước cutover tiếp theo, cần quyền admin).

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
- PostgreSQL (sau cutover): `pg_dump -Fc` (backup.sh tự nhận URL PG),
  restore bằng `pg_restore`.

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
| User báo bị khóa oan | `SELECT id,username,banned_at FROM users WHERE banned_at IS NOT NULL` | Unban từ trang mod (`/web/mod/reports`) khi xác nhận nhầm lẫn |
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

## 7. Cutover PostgreSQL (khi có quyền admin)

1. Chạy `scripts/backup.sh` (SQLite) + giữ lại file DB.
2. Tạo database + user PG mới; viết `TING_DATABASE_URL` PG vào `.env`.
3. `alembic upgrade head` (PG paths 0002–0007 đã viết nhưng chưa test — chạy trên DB rỗng, kiểm tra từng revision).
4. Import dữ liệu SQLite: `./.venv/bin/python -m ting_ting.migrate_data --source sqlite:///./ting_ting.db`
   (target phải rỗng; giữ ID, verify count từng bảng, reset sequence; từ chối ghi đè).
   Nếu không cần dữ liệu cũ: seed DB PG rỗng bằng `python -m ting_ting seed`.
5. `pytest -q` + live check, rồi restart service. SQLite file cũ giữ lại làm backup.

## 8. Mở khóa moderator (dev)

```sql
UPDATE users SET is_moderator = 1 WHERE username = '<user>';
```

(Nhớ xóa/đăng ký lại hoặc restart session nếu đang test: flag đọc mỗi request web.)