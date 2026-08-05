# Member Role Report — Day 9: Multi Agent A2A

## 1. Thông tin cá nhân

| Thông tin       | Nội dung                                                                 |
| --------------- | ------------------------------------------------------------------------ |
| Họ và tên       | Nguyễn Duy Hưng                                                          |
| MSSV            | 2A202601702                                                              |
| Khóa/Lớp        | K4                                                                       |
| Vai trò chính   | Data & Policy Engineer — tầng deterministic (data access, business rules, schema/verifier, baseline) |
| Ngày hoàn thành | 2026-08-05                                                               |

## 2. Vai trò và phạm vi công việc

### Phần việc sở hữu

| Module/deliverable | File/hàm phụ trách | Input nhận vào | Output bàn giao | Trạng thái |
| --- | --- | --- | --- | --- |
| Data access layer | `src/data_store.py` (`OlistStore`) | 5 CSV trong `data/` (orders, order_items, order_payments, customers, products) | Index in-memory theo `order_id`, `customer_unique_id`, `product_id` | Hoàn thành |
| Domain analyzers | `src/analysis.py` | Order/item/payment rows từ `OlistStore` | 5 hàm thuần: `analyze_delivery`, `reconcile_payments`, `resolve_customer`, `describe_products`, `summarize_order` | Hoàn thành |
| EC_POLICY_V2 engine | `src/policy.py` | Kết quả các analyzer | Phân loại 6 primary issue, secondary issues, responsible party, refund, root cause, evidence ids theo đúng thứ tự ưu tiên | Hoàn thành |
| Output schema + verifier | `src/schema.py` | Verdict từ `policy.apply_policy` + facts từ analyzer | Lắp document theo schema đề bài (array limit, null handling) và `verify_case_output` đối chiếu ngược lại CSV | Hoàn thành |
| Pipeline & baseline runner | `src/pipeline.py`, `run_baseline.py` | `input/EC_0xx.json` + `data/*.csv` | 50 file `output/EC_0xx.json` không dùng LLM, dùng làm ground truth cho tầng agent | Hoàn thành |

Đây là 5 module tôi trực tiếp viết và chạy; toàn bộ số học trong bài (giờ trễ giao hàng, đối soát BRL, evidence id) nằm ở đây — tầng agent của Tiên chỉ gọi lại các hàm này qua tool, không tự tính.

### Việc hỗ trợ ngoài phạm vi chính

| Hoạt động | Thành viên/module được hỗ trợ | Kết quả |
| --- | --- | --- |
| Thiết kế `build_policy_digest` (đưa boolean pre-computed thay vì count thô) | Nguyễn Hoàng Thảo Tiên / `src/agents.py` | Sau smoke test đầu tiên agreement 0/2, cùng debug và xác định model 8B không so ngưỡng số ổn định — đề xuất chuyển digest sang boolean, agreement lên 6/6 |
| Viết `scripts/compare_outputs.py` | Toàn nhóm | Công cụ diff từng field giữa `output/` (baseline) và `output_agents/` (multi-agent), dùng để xác nhận kết quả agent đúng 100% |

## 3. Kết quả theo vai trò

| Nhiệm vụ đã thực hiện | File/hàm/artifact liên quan | Kết quả bàn giao | Cách xác minh |
| --- | --- | --- | --- |
| Áp dụng EC_POLICY_V2 cho 50 case, không qua LLM | `src/policy.py`, `run_baseline.py` | `output/EC_001.json` .. `EC_050.json` | `python run_baseline.py` |
| Verifier độc lập kiểm tra ID/format/null-handling | `src/schema.py::verify_case_output` | 0 lỗi trên 50/50 case | Chạy lại `verify_case_output` độc lập cho từng file trong `output/` |

Output cụ thể: `run_baseline.py` chạy toàn bộ 50 case trong vài giây, không LLM, và là ground truth để đối chiếu tầng agent. Khi so hai thư mục bằng `scripts/compare_outputs.py`, kết quả là `identical: 50, differing: 0` — nghĩa là output multi-agent (Tiên phụ trách) tái tạo đúng 100% phán quyết deterministic của tôi.

## 4. Giải thích phần kỹ thuật đã thực hiện

### Vấn đề cần giải quyết

Đề giới hạn agent ở model ≤10B. Model cỡ đó không được tin để tự tính giờ trễ giao hàng hay đối soát BRL, và cũng không đủ ổn định để tự nhớ đúng thứ tự ưu tiên 6 nhánh policy. Đồng thời output phải khớp tuyệt đối với schema: đúng định dạng evidence id, đúng array limit, đúng cách xử lý `null` cho đơn hàng 0 item (6/50 case `unavailable`).

### Cách triển khai

Tách domain thành 5 hàm thuần, không side-effect, mỗi hàm chỉ nhận đúng slice dữ liệu nó cần — ví dụ `analyze_delivery` chỉ nhận `order` + `items`, không chạm `payments`. Việc tách này để về sau tầng agent expose từng hàm làm một tool riêng, gắn với quyền truy cập tách bạch theo agent. Policy áp theo thứ tự ưu tiên cứng: `canceled_order_paid`/`unavailable_order_paid` > `late_delivery_seller` > `late_delivery_logistics` > `valid_split_payment` > `unsupported_late_claim`. Toàn bộ số tiền dùng `Decimal` + `ROUND_HALF_UP` thay vì `round()` built-in của Python. Verifier chạy độc lập, đối chiếu ngược từng ID trong output với CSV gốc để bắt evidence "false positive".

### Input, output và contract

| Thành phần | Mô tả |
| --- | --- |
| Input | `input/EC_0xx.json` (`claimed_order_id`) + `data/*.csv` |
| Output | `output/EC_0xx.json` theo schema đề bài, kèm danh sách lỗi (rỗng khi pass) từ `verify_case_output` |
| Module phụ thuộc | Không — `analysis.py`/`policy.py` không phụ thuộc ngược lên tầng agent |
| Module sử dụng output | `src/orchestrator.py` (Coordinator) dùng chính các hàm này để lắp document cuối, bất kể model nói gì |
| Điều kiện lỗi cần xử lý | Order 0 item row: `expected_total_brl`/`difference_brl`/`reconciled` phải `null`, item/seller/product/handoff phải mảng rỗng |

### Cách xác minh

```bash
python run_baseline.py
```

- **Kết quả mong đợi:** 50 file ghi ra `output/`, verifier không báo lỗi.
- **Kết quả thực tế:** `wrote 50 cases -> output/` ... `verifier: all cases clean`; phân bố 6 nhánh: `late_delivery_seller` 10, `late_delivery_logistics` 10, `unsupported_late_claim` 8, `canceled_order_paid` 8, `valid_split_payment` 8, `unavailable_order_paid` 6 (tổng đúng 50).
- **Artifact/log:** `output/EC_001.json` .. `EC_050.json`.

## 5. Một quyết định kỹ thuật quan trọng

- **Bối cảnh:** đề không nói rõ điều kiện thêm action `verify_refund_completion` — dựa vào `refund_brl > 0` hay dựa vào loại issue?
- **Các phương án đã cân nhắc:** (1) thêm khi `recommended_refund_brl > 0`; (2) thêm chỉ khi primary issue thuộc nhóm hoàn toàn bộ (`canceled_order_paid`/`unavailable_order_paid`).
- **Phương án đã chọn:** (2).
- **Lý do:** ví dụ minh họa trong README (case `late_delivery_seller`, refund 18.27 BRL > 0) **không** có action `verify_refund_completion`. Nếu chọn (1) sẽ mâu thuẫn với chính ví dụ đề cho.
- **Bằng chứng quyết định phù hợp:** `output/EC_002.json` khớp từng field với ví dụ trong README (`delivery_variance_hours=87.39`, `handoff_variance_hours=1.04`, `recommended_refund_brl=18.27`, `evidence_ids` và `resolution_actions` trùng khớp, không có `verify_refund_completion`).

## 6. Một lỗi hoặc blocker đã xử lý

- **Triệu chứng/lỗi nguyên văn:** một test tay phát hiện số tiền bị lệch ở giá trị đúng giữa hai chữ số thập phân.
- **Lệnh hoặc bước tái hiện:** `round(0.125, 2)` trong Python trả về `0.12` thay vì `0.13`.
- **Nguyên nhân gốc:** hàm `round()` built-in của Python dùng banker's rounding (làm tròn tới số chẵn gần nhất khi đúng giữa .5), không phải "round half up" mà đề bài yêu cầu ("làm tròn 2 chữ số thập phân").
- **Cách xử lý:** viết `round2()` trong `analysis.py` dùng `Decimal.quantize(..., rounding=ROUND_HALF_UP)` cho mọi phép tính tiền và giờ.
- **Cách xác minh sau khi sửa:** chạy lại `run_baseline.py` cho toàn bộ 50 case, không còn case nào có `payment_reconciliation.difference_brl` lệch so với kỳ vọng thủ công.
- **Điều học được:** không dùng `round()` built-in cho tiền tệ trong bài chấm điểm tự động — cần biết rõ chuẩn làm tròn grader kỳ vọng.

## 7. Hiểu biết về luồng end-to-end

> Lưu ý: 5 câu hỏi gốc của template (Crossref, vector index, retrieval quality...) thuộc về một lab khác (RAG pipeline), có vẻ bị dán nhầm template. Tôi trả lời theo khái niệm tương đương của lab này.

**Câu trả lời:**

1. **Dữ liệu đi từ CSV Olist đến document cuối cùng như thế nào?** `input/EC_0xx.json` cho `claimed_order_id` → `OlistStore` load và index 5 CSV → các hàm trong `analysis.py` trả về facts thuần từ CSV (không suy diễn) → `policy.py` áp `EC_POLICY_V2` theo facts đó → `schema.py` lắp document theo đúng schema và verify ngược lại với CSV → ghi `output/EC_0xx.json`.
2. **Baseline nào dùng làm ground-truth để đo chất lượng phân loại?** `run_baseline.py` — bản thuần Python áp `EC_POLICY_V2` không qua LLM. `scripts/compare_outputs.py` diff từng field giữa `output/` (baseline) và `output_agents/` (multi-agent) để đo độ chính xác của tầng agent so với ground truth này.
3. **Quality check nào khác ngoài việc so khớp baseline?** `verify_case_output` trong `schema.py` — kiểm tra định dạng timestamp, tồn tại thật của mọi ID trong CSV gốc (chặn evidence "false positive"), giới hạn array, xử lý `null` cho đơn 0 item, và tính nhất quán giữa `case_status` với `recommended_refund_brl`. Check này chạy độc lập, không phụ thuộc vào việc so baseline.
4. **Vì sao phải dùng cùng 50 input case cho cả baseline và agent run?** Vì mục tiêu là đo agent có tái tạo đúng phán quyết deterministic hay không trên chính cùng dữ liệu. Nếu input khác nhau, chênh lệch kết quả có thể đến từ khác dữ liệu chứ không phải từ năng lực model — làm mất giá trị so sánh.
5. **Agent run được xem là thành công dựa trên artifact và metric nào?** Ba lớp: (a) `schema_failures = 0` từ `verify_case_output`; (b) `policy_agreement_rate` — 45/50 (90%) agent tự chọn đúng, 5 override được ghi minh bạch trong `trace.jsonl`, không giấu; (c) `scripts/compare_outputs.py` báo `identical: 50, differing: 0` khi so với baseline.

## 8. Cam kết của thành viên

Đánh dấu sau khi tự kiểm tra:

- [x] Nội dung báo cáo phản ánh đúng phần việc và mức hiểu của tôi.
- [x] Tôi có thể giải thích luồng end-to-end, không chỉ module mình phụ trách.
- [x] Tôi không ghi "đã chạy thành công" cho phần chưa được kiểm chứng.
- [x] Báo cáo không chứa `.env`, API key, token hoặc secret.
- [x] Báo cáo này không phải bản sao nguyên văn của báo cáo nhóm hoặc báo cáo thành viên khác.

**Họ và tên:** Nguyễn Duy Hưng
**Ngày xác nhận:** 2026-08-05
