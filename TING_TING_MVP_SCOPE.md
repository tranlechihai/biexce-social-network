# Ting Ting Social Backend MVP

> **SUPERSEDED (2026-08-20):** Đây là scope MVP ban đầu — giữ nguyên làm lịch
> sử, không phải nguồn sự thật hiện hành. Trạng thái, chức năng và roadmap
> hiện tại xem `PROJECT_STATUS_AND_ROADMAP.md` (product đã vượt scope MVP).

## 1. Mục tiêu

Xây dựng một MVP backend cho ứng dụng mạng xã hội mobile Ting Ting và một giao
diện web responsive tối giản để thao tác, kiểm thử luồng người dùng trên browser.

Đây là acceptance project chạy local/staging để đánh giá sản phẩm và workflow
BIEXCE Slim. Đây không phải bản production và không phải toàn bộ phạm vi trong
tài liệu BRD/SRS/ARS v1.0.

## 2. Người dùng và kết quả cần đạt

Người dùng có thể:

- Đăng ký, đăng nhập và đăng xuất.
- Xem và cập nhật profile cơ bản.
- Gửi, chấp nhận hoặc từ chối lời mời kết bạn.
- Hủy kết bạn và block người dùng.
- Tạo bài viết dạng text với audience `ONLY_ME` hoặc `FRIENDS`.
- Xem feed của bản thân và bạn bè, mới nhất trước.
- Like/unlike và thêm/xóa comment theo đúng quyền.
- Dùng giao diện web để thực hiện các luồng trên mà không cần gọi API thủ công.

## 3. Stack ưu tiên

- Python 3, FastAPI.
- SQLAlchemy và SQLite.
- Jinja2, HTML, CSS và JavaScript thuần cho web demo.
- Pytest cho unit test và integration test.
- Virtual environment và dependency file rõ ràng.
- Không dùng Docker hoặc frontend framework nếu không thật sự cần.

Nếu môi trường khiến một lựa chọn trên không khả dụng, Director được phép chọn
phương án đơn giản tương đương và ghi rõ trong plan trước Gate 1.

## 4. Phạm vi chức năng bắt buộc

### 4.1 Authentication và profile

- Username hoặc email là duy nhất.
- Mật khẩu phải được hash, không lưu plain text.
- Có session hoặc bearer token phù hợp cho API và web demo.
- Chỉ chủ tài khoản được cập nhật profile của mình.

### 4.2 Social graph

- Friend request có trạng thái pending, accepted hoặc rejected.
- Không cho gửi request cho chính mình.
- Không tạo duplicate friendship hoặc request đang chờ.
- Block phải ngăn hai bên xem nội dung và tương tác với nhau.
- Unblock không tự khôi phục friendship cũ.

### 4.3 Post, audience và feed

- Post MVP chỉ cần text.
- Chỉ tác giả được sửa hoặc xóa bài của mình.
- `ONLY_ME` chỉ tác giả đọc được.
- `FRIENDS` chỉ tác giả và bạn bè hiện còn hợp lệ đọc được.
- Quyền phải được kiểm tra tại read path; feed cache hoặc ID không tự cấp quyền.
- Feed sắp xếp ổn định theo thời gian mới nhất trước.
- Có phân trang đơn giản hoặc giới hạn kết quả hợp lý.

### 4.4 Like và comment

- Like/unlike phải idempotent.
- Người không có quyền đọc post không được like hoặc comment.
- Chủ comment hoặc chủ post được xóa comment; người khác bị từ chối.
- Counter và response phải nhất quán với dữ liệu lưu trữ.

### 4.5 Web demo

- Trang đăng ký và đăng nhập.
- Trang feed và form tạo bài.
- Trang profile và thao tác kết bạn/block.
- Nút like và form comment.
- Hiển thị validation/error rõ ràng.
- Responsive đủ dùng trên màn hình mobile và desktop.

### 4.6 Seed và tài liệu

- Seed ít nhất ba user, friendship, post và interaction mẫu.
- README có lệnh setup, seed, chạy server và chạy test.
- Có tài khoản demo hoặc hướng dẫn tạo tài khoản để nghiệm thu nhanh.

## 5. API và chất lượng tối thiểu

- REST endpoint có status code và error body nhất quán.
- Validate input, resource ownership và authorization ở server.
- Mutation quan trọng không tạo dữ liệu trùng khi request được gửi lại.
- Không hard-code secret hoặc credential vào source.
- Không ghi password, token hoặc dữ liệu nhạy cảm vào log.
- Code đơn giản, chia module theo trách nhiệm và tránh file quá lớn.

## 6. Kiểm thử bắt buộc

- Unit test cho rule social graph, audience và quyền sở hữu.
- Integration test cho auth, friend request, block, post, feed, like và comment.
- Negative test cho anonymous access, forbidden access và resource không tồn tại.
- Regression suite phải PASS trước Gate 2.
- Không xóa, bỏ qua hoặc làm yếu test chỉ để đạt PASS.
- Có một browser smoke checklist cho web demo.

## 7. Ngoài phạm vi MVP

- Ảnh, video, object storage và CDN.
- E2EE, key envelope, MLS hoặc device key lifecycle.
- Chat, call, realtime hoặc push notification.
- Circle, selected audience, mention, reply thread hoặc expiry.
- Public feed, recommendation, hashtag, trending hoặc quảng cáo.
- Moderation case, legal hold, privacy export/deletion orchestration.
- Microservices, event broker, distributed cache hoặc search cluster.
- Mobile application native.
- Production deployment, Kubernetes, HA, backup/PITR và compliance certification.

Các mục trên có thể được giữ dưới dạng extension point hoặc ghi trong phần
future work nhưng không được triển khai trong acceptance project này.

## 8. Workflow và human authority

- Director có thể hỏi tối đa ba câu nếu quyết định thật sự ảnh hưởng sản phẩm.
- Plan nên có khoảng 5-7 task triển khai được, không tạo task chỉ để viết process.
- Explore, Plan và Plan Review hoàn tất trước Gate 1.
- Sau Gate 1, task độc lập có thể chạy song song nếu không writer conflict.
- Mỗi phần phải được code, test và review; lỗi thông thường tự chuyển Fix.
- Chạy integration test và integration review trước Gate 2.
- Chỉ hỏi human ở Gate 1, Gate 2 hoặc quyết định source/sản phẩm có rủi ro thật.
- Metadata hoặc báo cáo không hoàn hảo không được chặn source đã kiểm chứng.
- User là authority cao nhất và có quyền đổi scope, ưu tiên hoặc dừng workflow.

## 9. Definition of Done

- Ứng dụng khởi động bằng hướng dẫn trong README.
- Seed chạy thành công trên database mới.
- Các luồng bắt buộc thao tác được qua API và web demo.
- Test suite và integration test PASS.
- BX Review không còn lỗi material.
- Gate 2 được user chấp nhận.

