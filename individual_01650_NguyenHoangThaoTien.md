# Member Role Report — Day 9: Multi Agent A2A

## 1. Thông tin cá nhân

| Thông tin       | Nội dung                                                                 |
| --------------- | ------------------------------------------------------------------------ |
| Họ và tên       | Nguyễn Hoàng Thảo Tiên                                                   |
| MSSV            | 2A202601650                                                              |
| Khóa/Lớp        | K4                                                                       |
| Vai trò chính   | Multi-Agent & Orchestration Engineer — tầng agent (LLM client, tool-calling, prompt, coordinator, trace) |
| Ngày hoàn thành | 2026-08-05                                                               |

## 2. Vai trò và phạm vi công việc

### Phần việc sở hữu

| Module/deliverable | File/hàm phụ trách | Input nhận vào | Output bàn giao | Trạng thái |
| --- | --- | --- | --- | --- |
| Model registry & config | `src/llm_config.py` | Ràng buộc ≤10B params từ đề bài | Danh mục 6 model OpenRouter ≤10B có tool calling; chọn `meta-llama/llama-3.1-8b-instruct`; `assert_within_param_cap()` chặn lúc khởi động | Hoàn thành |
| OpenRouter client | `src/llm_client.py` | `.env` (API key), payload chat completions | Client thuần `urllib` (không thêm dependency), retry + backoff, bắt riêng lỗi 402 hết credit, đếm token thread-safe | Hoàn thành |
| Tool layer & access matrix | `src/agent_tools.py` | `OlistStore` + tên agent gọi | 4 tool (`lookup_customer_history`, `lookup_order_items`, `reconcile_order_payments`, `analyze_order_delivery`) + `AGENT_TOOL_ACCESS` cưỡng chế quyền truy cập tại runtime | Hoàn thành |
| Agent prompts & handoff | `src/agents.py` | Kết quả tool từ 4 domain agent | Prompt cho Customer/Order&Product/Payment/Delivery/Policy/Verifier; `build_policy_digest` chuyển count thô thành boolean pre-computed | Hoàn thành |
| Coordinator & trace | `src/orchestrator.py`, `run_agents.py` | 50 case input, digest từ domain agent | Điều phối graph, logic retry/override khi Policy Agent sai, ghi `trace.jsonl` + `metadata.json` | Hoàn thành |

Tôi không chạm vào phần tính toán số học (đó là của Hưng) — công việc của tôi là điều phối 6 agent gọi đúng tool, đúng quyền, đúng thứ tự, và lắp document cuối cùng từ kết quả tool chứ không từ câu chữ model.

### Việc hỗ trợ ngoài phạm vi chính

| Hoạt động | Thành viên/module được hỗ trợ | Kết quả |
| --- | --- | --- |
| Debug agreement rate 0/2 ở smoke test đầu tiên | Nguyễn Duy Hưng / `src/agents.py`, `src/policy.py` | Cùng đọc trace, xác định model bịa `secondary_issues` vì phải tự so ngưỡng số; đổi digest sang boolean pre-computed, agreement lên 6/6 |
| Chạy `scripts/compare_outputs.py` sau mỗi lần sửa prompt | Nguyễn Duy Hưng | Xác nhận thay đổi ở tầng agent không làm output lệch khỏi baseline |

## 3. Kết quả theo vai trò

| Nhiệm vụ đã thực hiện | File/hàm/artifact liên quan | Kết quả bàn giao | Cách xác minh |
| --- | --- | --- | --- |
| Chạy multi-agent 50 case qua OpenRouter | `run_agents.py`, `src/orchestrator.py` | `logging/trace.jsonl` (1.486 dòng), `logging/metadata.json`, `output_agents/EC_001.json`..`EC_050.json` | `python run_agents.py --workers 5` |
| Đo agreement rate của Policy Agent | `src/orchestrator.py::_decide`, `_agreement_issues` | 45/50 (90%) case agent tự đồng ý với `EC_POLICY_V2`, 5 override được ghi minh bạch | Đếm event `policy_accepted`/`policy_override` trong `trace.jsonl` |

Output cụ thể: `metadata.json` ghi lại toàn bộ chỉ số của lượt chạy thật — 527 lượt gọi LLM, 224.782 token vào / 23.228 token ra, elapsed 364.5s, **0 lần phải ép gọi tool** (model tự chọn đúng tool ở toàn bộ 200 lượt gọi tool trong hệ thống).

## 4. Giải thích phần kỹ thuật đã thực hiện

### Vấn đề cần giải quyết

Điều phối 4 agent domain + 1 Policy + 1 Verifier chạy trên model 8B sao cho: (a) mỗi agent chỉ thấy đúng slice dữ liệu được phép — Delivery Agent không được đọc payment; (b) số liệu cuối cùng trong document không phụ thuộc việc model có tính đúng hay không; (c) khi model phân loại sai theo `EC_POLICY_V2` thì hệ thống phải phát hiện và xử lý minh bạch, không được lặng lẽ sửa mà giấu đi.

### Cách triển khai

Mỗi domain agent chạy 2 lượt gọi LLM: lượt 1 ép chọn tool (`tool_choice="required"`), lượt 2 đọc JSON kết quả tool và phát ra handoff. Coordinator lắp document cuối từ chính kết quả tool (`analysis.py` của Hưng), không từ câu chữ model viết ra. Policy Agent nhận một "digest" chỉ gồm các trường boolean cần thiết; verdict trả về bị đối chiếu cứng với bảng `EC_POLICY_V2` — sai thì retry một lần kèm lý do cụ thể (ví dụ: "secondary_issues wrong: included ['multi_seller_order'] whose input field is false"), vẫn sai thì override deterministic và ghi sự kiện `policy_override` vào trace thay vì bỏ qua.

### Input, output và contract

| Thành phần | Mô tả |
| --- | --- |
| Input | `order_id` + digest JSON (từ 4 domain agent) cho Policy Agent; document summary + schema errors cho Verifier Agent |
| Output | verdict `{primary_issue, secondary_issues, responsible_party_type}`; review `{approved, issues}` |
| Module phụ thuộc | `src/analysis.py`, `src/policy.py`, `src/schema.py` (chỉ gọi, không sửa) |
| Module sử dụng output | `run_agents.py` ghi `output_agents/`, `trace.jsonl`, `metadata.json` |
| Điều kiện lỗi cần xử lý | Model không gọi tool (`tool_forced` — coordinator tự gọi thay); model trả JSON không parse được (`parse_json_content` thử nhiều cách tách); OpenRouter trả 402 hết credit (`LLMError` ném ngay, không retry vô ích) |

### Cách xác minh

```bash
python run_agents.py --limit 6 --workers 3 --out output_agents
python scripts/compare_outputs.py output output_agents
```

- **Kết quả mong đợi:** agreement cao, 0 lỗi schema, output khớp baseline.
- **Kết quả thực tế:** smoke test 6 case đầu đạt agreement 6/6 (100%) sau khi sửa prompt; full run 50 case đạt 45/50 (90%), 50/50 verifier approved, 0 schema failure, diff với baseline `identical: 50, differing: 0`.
- **Artifact/log:** `logging/trace.jsonl`, `logging/metadata.json`, `output_agents/EC_001.json`..`EC_050.json`.

## 5. Một quyết định kỹ thuật quan trọng

- **Bối cảnh:** smoke test đầu tiên (2 case) cho agreement 0/2. Đọc trace thấy model chọn `primary_issue` gần đúng nhưng bịa `secondary_issues` — gán `multi_seller_order` cho một đơn chỉ có 1 seller.
- **Các phương án đã cân nhắc:** (1) đưa count thô (`item_count`, `seller_count`,...) vào digest và yêu cầu model tự so ngưỡng (`>= 2`) trong prompt; (2) tiền xử lý thành boolean đã tính sẵn (`multi_seller_order: true/false`) và chỉ yêu cầu model chọn field đang `true`.
- **Phương án đã chọn:** (2).
- **Lý do:** model 8B chọn đúng từ danh sách boolean có sẵn ổn định hơn nhiều so với tự suy luận ngưỡng số trong đầu — đây là giới hạn năng lực thực tế của model nhỏ, không giải quyết được chỉ bằng viết lại câu chữ prompt.
- **Bằng chứng quyết định phù hợp:** agreement nhảy từ 0/2 (0%) lên 6/6 (100%) ngay sau khi đổi digest từ count sang boolean, giữ nguyên model và toàn bộ phần còn lại của hệ thống không đổi.

## 6. Một lỗi hoặc blocker đã xử lý

- **Triệu chứng/lỗi nguyên văn:** Ollama từng được cài trên máy (registry Windows ghi nhận "Ollama version 0.20.5") nhưng executable, thư mục cài `...\Programs\Ollama\` và toàn bộ model đã tải trong `~/.ollama` đều không còn — server cổng `11434` không phản hồi.
- **Lệnh hoặc bước tái hiện:** `Get-Command ollama` → not found; `Invoke-WebRequest http://127.0.0.1:11434/api/tags` → server not responding; quét toàn ổ C tìm `ollama.exe` → 0 kết quả.
- **Nguyên nhân gốc:** Ollama đã bị gỡ cài đặt khỏi máy (entry uninstall trong registry còn sót lại nhưng binary và model store đã bị xóa thật), không phải lỗi cấu hình hay PATH.
- **Cách xử lý:** chuyển toàn bộ 7 agent sang gọi qua OpenRouter API — model `meta-llama/llama-3.1-8b-instruct` (8B, có tool calling, giá $0.05/$0.08 mỗi 1M token) thay vì chạy local qua Ollama.
- **Cách xác minh sau khi sửa:** gọi thử endpoint `/api/v1/key` và `/chat/completions` bằng key thật trước khi chạy pipeline — 3 model test đều trả lời hợp lệ; sau đó chạy full 50 case thành công, tổng chi phí thực tế dưới $0.15 (224.782 token vào, 23.228 token ra).
- **Điều học được:** không giả định môi trường local còn nguyên trạng chỉ vì registry nói đã cài — cần probe thực tế (process đang chạy, port mở, file tồn tại trên đĩa) trước khi dựng cả pipeline phụ thuộc vào nó.

## 7. Hiểu biết về luồng end-to-end

> Lưu ý: 5 câu hỏi gốc của template (Crossref, vector index, retrieval quality...) thuộc về một lab khác (RAG pipeline), có vẻ bị dán nhầm template khi tạo file. Tôi trả lời theo khái niệm tương đương của lab multi-agent này.

**Câu trả lời:**

1. **Dữ liệu đi từ input case đến document cuối cùng qua bao nhiêu lượt LLM?** `claimed_order_id` được Coordinator phát cho 4 domain agent, mỗi agent chạy 2 lượt gọi model (chọn tool → đọc kết quả và phát handoff). 4 handoff được gộp thành 1 digest gửi cho Policy Agent (1 lượt, có thể retry thêm 1 lượt nếu sai). Document được Coordinator lắp từ kết quả tool, sau đó gửi cho Verifier Agent (1 lượt) duyệt trước khi ghi ra `output/`. Tổng tối đa khoảng 12 lượt gọi model cho mỗi case.
2. **Baseline nào dùng để đo chất lượng của tầng agent tôi phụ trách?** `run_baseline.py` của Hưng — bản deterministic không qua LLM, đóng vai trò ground truth. Tôi dùng `scripts/compare_outputs.py` để diff `output/` với `output_agents/` sau mỗi thay đổi prompt, và dùng `policy_agreement_rate` trong `metadata.json` để đo riêng năng lực phân loại của Policy Agent trước khi bị coordinator can thiệp.
3. **Quality check nào nằm trong tầng orchestration, khác với việc so baseline?** Hai lớp: `_agreement_issues` đối chiếu verdict của Policy Agent với bảng `EC_POLICY_V2` ngay trong lúc chạy (không phải hậu kiểm), và Verifier Agent duyệt lại document cuối cùng kèm kết quả `schema_errors` trước khi coordinator cho phép ghi file.
4. **Vì sao phải dùng cùng 50 input case cho cả baseline và agent run?** Vì mục tiêu là đo agent có tái tạo đúng phán quyết deterministic hay không trên chính cùng dữ liệu; nếu input khác nhau, chênh lệch có thể đến từ khác dữ liệu chứ không phải từ năng lực model, làm mất giá trị so sánh.
5. **Agent run được xem là thành công dựa trên artifact và metric nào?** `metadata.json` — `policy_agreement_rate` (45/50 = 90%), `schema_failures = 0`, `policy_overrides` (5, ghi công khai chứ không giấu); và `scripts/compare_outputs.py` báo `identical: 50, differing: 0` so với baseline của Hưng.

## 8. Cam kết của thành viên

Đánh dấu sau khi tự kiểm tra:

- [x] Nội dung báo cáo phản ánh đúng phần việc và mức hiểu của tôi.
- [x] Tôi có thể giải thích luồng end-to-end, không chỉ module mình phụ trách.
- [x] Tôi không ghi "đã chạy thành công" cho phần chưa được kiểm chứng.
- [x] Báo cáo không chứa `.env`, API key, token hoặc secret.
- [x] Báo cáo này không phải bản sao nguyên văn của báo cáo nhóm hoặc báo cáo thành viên khác.

**Họ và tên:** Nguyễn Hoàng Thảo Tiên
**Ngày xác nhận:** 2026-08-05
