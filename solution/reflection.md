# Reflection (≤1 trang)

**Kết quả**

| Phiên bản | Practice | Public | **Private** |
|---|---|---|---|
| v1: ngưỡng z ≥ 2.5σ, kiểm định hai phía | 50.0 (TPR 1.00 / FPR 0.00) | 45.12 (0.95 / 0.017) | 32.41 (0.648 / 0.000) |
| **v2 (nộp): z ≥ 2.0σ, một phía theo hướng lỗi, có prior** | 49.31 (1.00 / 0.023) | 46.45 (1.00 / 0.058) | **32.72 (0.667 / 0.021)** |

## 1. Loại lỗi nào khó phát hiện nhất trong 5 luồng sự kiện?

- **Dễ phát hiện:** các lỗi *cấu trúc*, vì công cụ trả về tín hiệu rời rạc, không lẫn vào nhiễu.
  - `contract_checkpoint`: có `violations` (schema/type), hoặc độ trễ vượt SLA đã khai báo.
  - `lineage_run`: thiếu cạnh upstream, hoặc output mồ côi (`downstream_count = 0`).
- **Khó nhất:** các lỗi *số liệu ở mức tinh vi*.
  - Ở `data_batch`: null-rate/volume/freshness/phân phối tăng nhẹ.
  - Ở `lineage_run`: runtime chậm hơn một chút.
  - Ở `embedding_batch`: embedding drift hoặc corpus staleness nằm sát vùng dữ liệu sạch.
- **Vì sao khó:**
  - Payload của mọi sự kiện đều giống hệt nhau, nên tín hiệu duy nhất là giá trị metric của chính sự kiện đó.
  - Bằng chứng từ phase private: hạ ngưỡng từ 2.5σ xuống 2.0σ chỉ tăng TPR từ 0.648 lên 0.667. Nghĩa là khoảng 1/3 số lỗi private nằm dưới 2σ, tức là trong vùng phân phối sạch của một metric. Một phép kiểm định trên từng sự kiện riêng lẻ không tách được chúng nếu không chấp nhận báo nhầm nhiều hơn hẳn.

## 2. Cân bằng giữa chi phí gọi `ctx.tools` và TPR

- **Chi phí:** mỗi sự kiện gọi đúng **một** công cụ, là công cụ duy nhất nhìn thấy trạng thái của sự kiện đó. Trung bình khoảng 1.5 credit/sự kiện.
  - Private: khoảng 300/320, không vượt ngân sách.
  - Public: 240/220, vượt 9%, bị trừ khoảng 1.8 điểm.
  - Tôi vẫn không bỏ cuộc gọi nào: mỗi credit vượt chỉ tốn khoảng 0.09 điểm, còn bỏ sót một lỗi tốn khoảng 1.3 điểm. Bao phủ đầy đủ một lượt là lựa chọn đúng.
- **Ngưỡng cảnh báo:** suy ra từ công thức điểm.
  - Bỏ sót một lỗi bị trừ 0.5/n_faulty, báo nhầm một sự kiện sạch bị trừ 0.3/n_clean. Sự kiện sạch nhiều gấp khoảng 3 lần sự kiện lỗi.
  - Vì vậy nên cảnh báo khi khả năng là lỗi lớn hơn khoảng 0.6 lần khả năng là sạch, tương ứng khoảng **2σ** chứ không phải 3σ như baseline.
  - Hạ thấp hơn (1.6–1.8σ) làm số báo nhầm trên practice tăng gấp 3 mà không bắt thêm lỗi nào, nên tôi dừng ở 2.0σ.
- **Thống kê:**
  - σ lấy từ `ctx.baseline` (mean ± 3σ).
  - Kiểm định một phía theo hướng lỗi (spike/lag/drift/staleness đều là *tăng*). Chiều ngược lại vẫn kiểm tra nhưng ở mức 3σ.
  - Ngoài ra, cảnh báo khi tổng z² của nhiều metric cùng tăng vừa phải.

## 3. Nếu có thêm thời gian: cải tiến cơ chế thống kê nào trong `ctx.state`?

Hiện tại `ctx.state` lưu hai thứ:
- (a) lịch sử giá trị của các sự kiện được đánh giá là sạch, dùng để tính median trực tuyến, trộn với giá trị trung tâm ban đầu (prior);
- (b) tần suất các cạnh upstream của từng job.

Tôi sẽ cải tiến:
- **CUSUM/EWMA cho từng metric:** cộng dồn độ lệch nhỏ qua nhiều sự kiện liên tiếp để bắt các drift dưới 2σ mà kiểm định từng sự kiện bỏ lỡ.
- **Ước lượng σ trực tuyến (MAD)**, trộn với σ của baseline thay vì chỉ dùng baseline. Ví dụ σ thực của runtime lineage khoảng 232 ms, trong khi baseline ngầm định 337 ms. Ngưỡng sẽ chặt hơn mà không tăng báo nhầm.
- **Gộp các metric của một sự kiện thành một tỉ số likelihood duy nhất**, thay vì max-z cộng thêm χ² dự phòng. Ngưỡng khi đó đặt thẳng theo tỉ lệ chi phí 0.5 : 0.3 của công thức điểm.
