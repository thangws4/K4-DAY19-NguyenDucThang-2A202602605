# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Nguyễn Đức Thắng  **MSSV:** 2A202602605  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.

Cấu hình: chat `gemini:gemini-3.5-flash-lite`, embedding `gemini:gemini-embedding-001`, `top_k=3`, `chunk_size=800`, 176 chunk. Ontology **tự thiết kế** (`report/ONTOLOGY.md`). Bản đối chứng chạy bằng ontology gợi ý, cùng provider, nằm ở `ket_qua_benchmark_kg.hint.txt`.

## 1. Chi phí (10 điểm)

```
Chat model: gemini:gemini-3.5-flash-lite | Embedding: gemini:gemini-embedding-001 | top_k=3 | chunk_size=800 | chunks=176 | KG: 197 nodes / 600 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000    118.7
graph       196     39139     5350   0.00000    156.6

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.51   1.50      696       72   0.00000     4.07
graph       0.89   2.00     2518      119   0.00000     6.06
```

**Cột USD bằng 0 không có nghĩa là miễn phí.** `src/llm.py` không có giá cho `gemini-3.5-flash-lite`, nên `price()` trả 0. Embedding Gemini cũng không trả số token (`in_tok = 0` ở dòng flat). Vì vậy tôi so chi phí bằng **token**. Cột "USD ước tính" bên dưới tự tính từ token theo giá `gemini-2.5-flash-lite` trong bảng của `src/llm.py` ($0,10 / $0,40 mỗi 1M token vào/ra). Đây là **ước tính**, không phải số trong file.

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD (file) | 0.00000 | 0.00000 | không tính được (giá chưa có trong bảng) |
| Indexing USD (ước tính từ token) | ~0 (chỉ embedding, không có số token) | 0,00605 (39 139 in + 5 350 out) | phần tăng thêm ≈ $0,006 |
| Indexing giây | 118.7 | 156.6 | ×1,32 |
| Indexing LLM chat calls | 0 | 20 | +20 lần gọi trích xuất |
| Mỗi câu: USD (file) | 0.00000 | 0.00000 | không tính được |
| Mỗi câu: USD (ước tính từ token) | 0,000098 | 0,000299 | ×3,0 |
| Mỗi câu: giây | 4.07 | 6.06 | ×1,49 (xem ghi chú) |
| Mỗi câu: in_tok | 696 | 2518 | ×3,62 |
| Mỗi câu: out_tok | 72 | 119 | ×1,65 |

*Ghi chú về giây:* Gemini free tier giới hạn 15 request/phút. Khi bị 429, client tự chờ rồi thử lại, và thời gian chờ bị tính vào cột `seconds`. Hai câu bị ảnh hưởng rõ là Q1 graph (24.02s) và Q4 flat (16.36s). Bỏ mỗi pipeline một câu bị nghẽn đó thì trung bình còn: flat ≈ 1,62s, graph ≈ 2,47s (×1,5). Tỉ lệ vẫn giữ, nhưng số tuyệt đối trong file bị thổi phồng.

**Chi phí tăng thêm đến từ đâu?**
> - *Indexing:* toàn bộ phần tăng (39 139 token vào, 5 350 token ra) là **20 lần gọi LLM trích xuất tin tức**. Phần luật dựng bằng regex nên không tốn token nào. Phần embedding giống hệt Flat vì GraphRAG dùng lại cùng vector index.
> - *Mỗi câu hỏi:* phần tăng là **prompt dài hơn**: thêm ~1 800 token dữ kiện graph (tóm tắt vụ, thang khung phạt, khoản khớp ngưỡng). Output chỉ tăng ×1,65 vì câu trả lời dẫn thêm Điều/khoản.
> - *So với ontology gợi ý* (`ket_qua_benchmark_kg.hint.txt`): bản gợi ý tốn **5 589** token vào mỗi câu, vì đưa nguyên `text` của mọi khoản nhắc tới chất. Bản của tôi chỉ đưa dòng khung phạt và đúng khoản khớp ngưỡng, nên còn **2 518** (−55%).
>
> *Điểm hòa vốn:* tính theo chi phí thì GraphRAG không bao giờ rẻ hơn Flat: trả thêm ≈ $0,006 một lần và ≈ $0,0002 mỗi câu (ước tính). Cái đổi lại là chất lượng: trên 3 câu cross-kb (Q3–Q5), recall tăng từ 0,35 lên 1,00 và judge từ 1 lên 2. Với 1 000 câu hỏi, phần trả thêm chỉ khoảng $0,2 + $0,006, rất nhỏ so với giá trị của một câu trả lời đúng khung hình phạt. Vậy nên lý do để không dùng KG là **độ phức tạp vận hành** (Neo4j, prompt trích xuất, lỗi im lặng ở mục 3), không phải tiền.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Định nghĩa nằm nguyên trong một chunk Điều 2 Luật PCMT; graph không có gì thêm (Luật PCMT không có `Crime`) |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Tên hai bị cáo tử hình nằm ngay trong đoạn đầu bài báo, vector search lấy được |
| Q3 | cross-kb | 0.33 / 1 | 1.00 / 2 | **Graph** | Flat có "36 tháng tù" nhưng chunk luật không vào top-3 nên trả lời "không đủ thông tin" về Điều; graph đi `Person → Case → Crime → Điều 251 khoản 1` |
| Q4 | cross-kb | 0.33 / 1 | 1.00 / 2 | **Graph** | Thang khung phạt của Điều 255 có khoản 4 "tù 20 năm hoặc tù chung thân"; bản gợi ý chỉ lấy khoản 1 nên được 0.67 / 1 |
| Q5 | cross-kb-multi-hop | 0.40 / 1 | 1.00 / 2 | **Graph** | Cạnh `THRESHOLD` chọn đúng một khoản: "MDMA hơn 9,6kg thuộc Điều 250 khoản 4 điểm b"; Flat không có văn bản luật |
| Q6 | aggregation | 0.00 / 2 | 0.33 / 2 | **Graph** (về nội dung) | Graph liệt kê đủ 3 vụ trong đáp án chuẩn; Flat chỉ có 2 vụ (tách vụ Cái Quang Huy làm hai) và sót vụ Viện Pháp y. Recall thấp ở cả hai do phép đo (E4, mục 3) |

**Quy luật:** loại câu hỏi quyết định bên thắng.
- **Single-hop** (Q1, Q2): đáp án nằm trong **một** đoạn văn bản, nên Flat đủ và graph chỉ làm prompt dài thêm.
- **Cross-kb** (Q3–Q5): đáp án nằm ở **hai** tài liệu không giống nhau về từ ngữ. Câu hỏi về Lê Minh Thành không chứa chữ nào của Điều 251, nên vector search không bao giờ kéo được chunk luật vào top-3. Chỉ có đường đi qua node cầu nối `Crime` mới nối được. Flat đạt recall trung bình 0,35, Graph đạt 1,00.
- **Aggregation** (Q6): cần **liệt kê đủ**, trong khi top-3 chunk tối đa chỉ phủ được 3 đoạn. Graph trả về mọi `Case -INVOLVES-> MDMA` (5 vụ), không bị giới hạn bởi top-k.

## 3. Phân tích lỗi (20 điểm)

### Lỗi E1: Cầu nối gãy hàng loạt vì trích xuất thất bại im lặng

- **Hiện tượng:** ở lần chạy `--judge` đầu tiên với ontology của tôi, graph có đúng 1 `Case` trên 20 bài báo. GraphRAG mất lợi thế cross-kb: Q4 và Q5 trả lời "không đủ thông tin", recall của graph rơi từ 0,89 xuống 0,68. Lệnh chạy không báo lỗi nào.
- **Bằng chứng:** file kết quả của lần chạy đó được giữ lại ở `report/evidence/ket_qua_benchmark_kg.failed_extraction.txt`.

  Dòng đầu file: `... | KG: 147 nodes / 512 rels`. Con số này bằng đúng graph "luật + 1 bài" của `--check`; graph đầy đủ là 197 nodes / 600 rels. Bảng Querying: `graph 0.68 1.67 1551 ...`.

  Trích câu trả lời Q5, pipeline graph, trong file đó:
  > "Ngữ cảnh không đủ thông tin để xác định số Điều luật, khoản áp dụng cũng như khung hình phạt cụ thể đối với tội vận chuyển trái phép chất ma túy của Cái Quang Huy (các Điều luật trong dữ kiện chỉ quy định về 'Tội mua bán trái phép chất ma túy' tại Điều 251 BLHS)."

  Truy vấn ngay sau lần chạy đó:

  ```cypher
  MATCH (k:Case) RETURN k.id
  ```
  ```
  [{'k.id': 'news-100260918080821054#0'}]      -- chỉ còn bài Lê Minh Thành
  ```

  Lần chạy lại (file nộp) cho 12 `Case`. 3 vụ không có `CHARGED_WITH` đều là **gãy đúng**:

  ```cypher
  MATCH (k:Case) WHERE NOT (k)-[:CHARGED_WITH]->() RETURN k.name, k.doc_id
  ```
  ```
  Vụ vận chuyển vũ khí và hơn 800kg chất nghi ma túy tại Preah Sihanouk, Campuchia | news-100260924145818945   (luật Campuchia, không thuộc BLHS)
  Vụ tông cảnh sát giao thông tại An Giang                                         | news-100260926112415229   (không có tội ma túy)
  Vụ phát hiện bao tải chứa 20kg nghi ma túy trôi dạt bờ biển Phú Quốc              | news-100260927182621527   (chưa có nghi phạm, chưa có tội danh)
  ```
- **Nguyên nhân:** lỗi nằm ở bước **trích xuất (code xử lý output LLM)**, không phải ở ontology. Hàm trích xuất bọc `json.loads(...)` trong `except JSONDecodeError: return []`. Khi provider trả về nội dung rỗng hoặc không phải JSON, bài báo bị bỏ khỏi graph mà không có dấu hiệu gì. Gọi lại cùng 20 prompt ngay sau đó thì cả 20 đều parse được (11 bài có vụ, 9 bài không có vụ), nên lỗi là **chập chờn ở phía provider**. Giả thuyết của tôi là Gemini đôi khi trả nội dung rỗng với văn bản về ma túy (bộ lọc an toàn) hoặc lỗi tạm thời, nhưng tôi không tái hiện được nên không khẳng định.
- **Đề xuất sửa:** (đã làm trong `src/graph.py`, `extract_cases`)
  1. Thử lại tối đa 3 lần khi output không phải JSON hợp lệ, và in `[cảnh báo] <doc_id>: LLM trả JSON không hợp lệ` ra stderr để lỗi không còn im lặng. Lần chạy lại không có cảnh báo nào và graph đủ 12 `Case`.
  2. Nên làm thêm: sau `build_graph`, kiểm tra tỉ lệ bài có `Case`, và dừng hẳn nếu tỉ lệ bất thường.

  *Đánh đổi:* mỗi lần thử lại tốn thêm một lần gọi LLM (khoảng 2k token). Nhưng một graph thiếu 95% dữ liệu còn đắt hơn nhiều, vì mọi số liệu benchmark sau đó đều vô nghĩa.

### Lỗi E2: Thiếu ngữ cảnh luật — quy tắc lọc khoản bỏ sót khung cao nhất (ontology gợi ý, Q4)

- **Hiện tượng:** với ontology gợi ý, GraphRAG trả lời đúng hành vi của "Hoàng Nato" nhưng nói không đủ thông tin về mức phạt tối đa, dù graph có đủ 5 khoản của Điều 255.
- **Bằng chứng:** `ket_qua_benchmark_kg.hint.txt`, Q4, pipeline graph, `recall=0.67 judge=1`:
  > "Mức phạt tù tối đa: Không đủ thông tin cụ thể … (ngữ cảnh chỉ cung cấp [Điều 255 BLHS - Tội tổ chức sử dụng trái phép chất ma túy] khoản 1 với mức phạt tù từ 02 năm đến 07 năm, không có thông tin về các khoản nặng hơn)."

  Chạy quy tắc lọc của Bước 5 trên graph gợi ý (`report/evidence/hint_graph_queries.txt`):

  ```cypher
  MATCH (p:Person)-[:INVOLVED_IN]->(k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
  WHERE (p.name CONTAINS "Nato" OR any(x IN coalesce(p.aliases,[]) WHERE x CONTAINS "Nato"))
    AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
  RETURN DISTINCT a.id, cl.number, cl.penalty ORDER BY a.id, cl.number
  ```
  ```
  Điều 249 BLHS khoản 1..4   (tàng trữ — tội của cả chuyên án 126 người, không phải của Hoàng Nato)
  Điều 251 BLHS khoản 1..4   (mua bán — như trên)
  Điều 255 BLHS khoản 1      <- chỉ khoản 1; khoản 4 "tù 20 năm hoặc tù chung thân" bị loại
  ```
- **Nguyên nhân:** lỗi nằm ở **Cypher KG-3 (quy tắc lọc) kết hợp với thiết kế ontology**. Quy tắc "khoản 1 + khoản `MENTIONS` chất của vụ" mặc định rằng khung hình phạt tăng theo khối lượng chất. Điều này đúng với Điều 249–252 nhưng **sai với Điều 255**: khung của Điều 255 tăng theo số người hoặc mức tổn hại sức khỏe, và không khoản nào nhắc tên chất. Ngoài ra, quy tắc đi theo **mọi** tội của vụ, nên kéo về 8 khoản Điều 249/251 không liên quan tới người được hỏi.
- **Đề xuất sửa:** (đã làm trong ontology và KG-3 của tôi)
  1. Đưa vào context **thang khung phạt**, tức dòng đầu của mọi khoản trong Điều, để luôn có cả khung cơ bản lẫn khung cao nhất.
  2. Khi câu hỏi nêu tên một người, chỉ đi theo `INVOLVED_IN.charge` của người đó.

  Kết quả trong `ket_qua_benchmark_kg.txt`: Q4 graph `recall=1.00 judge=2`, câu trả lời "… phạt tù tối đa là 20 năm hoặc tù chung thân (theo khoản 4)". *Đánh đổi:* thêm khoảng 5 dòng cho mỗi Điều. Nhưng vì chỉ lấy dòng khung phạt thay vì nguyên văn khoản, tổng prompt vẫn giảm từ 5 589 xuống 2 518 token mỗi câu so với bản gợi ý.

### Lỗi E4: Phép đo sai — recall và judge mâu thuẫn ở Q6

- **Hiện tượng:** ở Q6, cả hai phép đo đều sai, theo hai hướng ngược nhau:
  - Flat: `recall=0.00` nhưng `judge=2`. Judge quá dễ dãi.
  - Graph: `recall=0.33` nhưng `judge=2`. Recall quá khắt khe, câu trả lời thực ra đúng.
- **Bằng chứng:** `ket_qua_benchmark_kg.txt`, Q6. `must_include` = `["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]`. Đáp án chuẩn có 3 vụ: Cái Quang Huy, Lê Minh Thành, Viện Pháp y tâm thần.

  Flat (`recall=0.00 judge=2`):
  > "1. … MDMA (khối lượng gần 4,3kg) liên quan đến các nhân vật như Đạt và Huy. 2. … Thành bị bắt quả tang … 3. … viên nén hình tam giác màu hồng - xám … MDMA (khối lượng hơn 5,3kg) liên quan đến Đức và Huy."

  Câu trả lời này tách **một** vụ Cái Quang Huy thành hai (hai kiện hàng), **sót** vụ Viện Pháp y, và gọi tên tắt "Huy", "Thành". Nó chỉ đúng một phần (2/3 vụ), nhưng judge vẫn cho 2.

  Graph (`recall=0.33 judge=2`):
  > "1. Vụ vận chuyển hơn 10kg ma túy từ Đức về Việt Nam qua sân bay Nội Bài (… MDMA hơn 9,6kg …) 2. Vụ mua bán ma túy liên quan đến 3 thanh niên kháng cáo tại Hà Nội (… MDMA 5 viên) … 4. Vụ sai phạm tại Viện Pháp y tâm thần Trung ương … 5. Vụ án xảy ra tại Viện Pháp y tâm thần Trung ương (… MDMA 0,686g)."

  Câu trả lời này có đủ 3 vụ trong đáp án chuẩn (thêm vụ Hoàng Nato, mục 3), nhưng gọi vụ theo **tên vụ** chứ không theo họ tên người, nên chỉ khớp được "Pháp y tâm thần".

  Cypher trả lời thẳng câu hỏi, để đối chiếu:

  ```cypher
  MATCH (s:Substance {name:"MDMA"})<-[i:INVOLVES]-(k:Case) RETURN k.name, k.doc_id, i.amount ORDER BY k.doc_id
  ```
  ```
  Vụ vận chuyển hơn 10kg ma túy từ Đức về Việt Nam qua sân bay Nội Bài | news-100260917203001265 | hơn 9,6kg
  Vụ mua bán ma túy liên quan đến 3 thanh niên kháng cáo tại Hà Nội    | news-100260918080821054 | 5 viên
  Vụ bắt giang hồ 'Hoàng Nato' và 126 người liên quan 8 đường dây ma túy | news-100260920221957595 |
  Vụ sai phạm tại Viện Pháp y tâm thần Trung ương và tiệc ma túy ở Sầm Sơn | news-100260924105118645 |
  Vụ án xảy ra tại Viện Pháp y tâm thần Trung ương                     | news-100260930085028036 | 0,686g
  ```
  Vụ Hoàng Nato có MDMA vì bài viết "mua bán ma túy loại etomidate, ketamine, **thuốc lắc**", và thuốc lắc = MDMA. Như vậy graph có lý, còn đáp án chuẩn bỏ sót vụ này.
- **Nguyên nhân:** lỗi nằm ở **phép đo**, không phải ở pipeline.
  - `keyword_recall` so chuỗi con chính xác, nên một câu trả lời đúng nhưng diễn đạt khác (tên vụ thay vì tên người, "Huy" thay vì "Cái Quang Huy") bị chấm 0.
  - LLM-judge chỉ có một thang 0–2 tổng quát, không có danh sách ý bắt buộc. Nó thấy "3 vụ có MDMA" là cho 2, không kiểm tra từng vụ có đúng là vụ trong đáp án không.

  Hệ quả: nếu chỉ nhìn bảng tổng, recall trung bình của bản gợi ý (0.94) cao hơn của tôi (0.89), mà toàn bộ chênh lệch nằm ở đúng câu Q6 này (gợi ý 1.00 vs 0.33). Trong khi đó judge lại xếp ngược lại (1.83 vs 2.00).
- **Đề xuất sửa:**
  1. `must_include` cho phép **nhiều cách viết** cho mỗi ý, ví dụ `[["Cái Quang Huy", "vận chuyển hơn 10kg", "Đức về Việt Nam"], …]`, và tính recall theo ý chứ không theo chuỗi.
  2. Prompt judge đưa **danh sách ý bắt buộc**, yêu cầu chấm từng ý (có / không) rồi mới cộng điểm; với câu aggregation thì trừ điểm khi sót mục.

  *Đánh đổi:* (1) phải viết đáp án kỹ hơn bằng tay; (2) judge tốn thêm khoảng 100–200 token output mỗi câu, và vẫn là LLM nên vẫn có thể sai, chỉ là dễ kiểm tra hơn.

### Lỗi E5: LLM gán sai chất, rồi cạnh `THRESHOLD` biến lỗi đó thành kết luận pháp lý sai

- **Hiện tượng:** trong graph của tôi, vụ "Hoàng Nato và 126 người" có `INVOLVES -> Methamphetamine {amount: "khoảng 100g"}`. Nhưng bài báo chỉ viết "khoảng 100g ma túy tổng hợp **các loại**", không nói loại nào.
- **Bằng chứng:**

  ```cypher
  MATCH (k:Case {doc_id:"news-100260920221957595"})-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
        -[t:THRESHOLD]->(s:Substance)<-[i:INVOLVES]-(k)
  WHERE i.grams >= t.min_g AND (t.max_g IS NULL OR i.grams < t.max_g)
  RETURN a.id, cl.number, t.point, s.name, i.amount, cl.penalty
  ```
  ```
  Điều 251 BLHS | 4 | b | Methamphetamine | khoảng 100g | phạt tù 20 năm, tù chung thân hoặc tử hình
  Điều 249 BLHS | 4 | b | Methamphetamine | khoảng 100g | phạt tù từ 15 năm đến 20 năm hoặc tù chung thân
  ```
  Câu gốc, `data/drug_news/news-100260920221957595.md` dòng 34: "Tang vật thu giữ gồm hơn 1.000 đầu pod chill chứa ma túy etomidate, khoảng 100g ma túy tổng hợp các loại …".

  Lỗi lặp lại ở lần dựng graph sau (graph dùng để chụp ảnh, `report/img/kg_my_case.png`: node `Substance` "Methamphetamine" gắn với vụ 36kg):

  ```cypher
  MATCH (k:Case {doc_id:"news-100260928173914514"})-[i:INVOLVES]->(s) RETURN s.name, i.amount, i.grams
  ```
  ```
  Methamphetamine | hơn 36kg | 36000.0
  ```
  Trong khi bài gốc (`news-100260928173914514.md`, dòng 20 và 34) chỉ viết "hơn 34kg ma túy các loại" / "hơn 36kg ma túy các loại". Ở vụ này, khoản 4 tình cờ vẫn đúng vì tổng khối lượng vượt mọi ngưỡng, nhưng graph đã đưa ra kết luận đúng từ một lý do sai.
- **Nguyên nhân:**
  - *Prompt trích xuất:* prompt yêu cầu "dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", và LLM ép "ma túy tổng hợp các loại" vào một tên chuẩn. `canonical_substance` không chặn được, vì LLM đã trả sẵn chuỗi `"Methamphetamine"`.
  - *Thiết kế ontology:* `THRESHOLD` so sánh khối lượng một cách máy móc, nên một lỗi trích xuất nhỏ thành ngay "khoản 4, tới tử hình". Không có nó thì LLM trả lời còn có thể nhận ra chỗ mơ hồ. Ontology gợi ý gặp cùng lỗi gán chất (Methamphetamine có 5 vụ trong graph gợi ý), nhưng hậu quả nhẹ hơn vì nó không tự suy ra khoản.
- **Đề xuất sửa:**
  1. Thêm `Substance` "ma túy tổng hợp (không rõ loại)" và cho phép LLM dùng tên đó.
  2. Thêm trường `evidence` (câu trích nguyên văn) cho mỗi chất. Code kiểm tra tên chất hoặc alias có xuất hiện trong câu trích không; không có thì không tạo `grams`, nên `THRESHOLD` không kích hoạt.

  *Đánh đổi:* output trích xuất dài thêm khoảng 20–30% token, và một số vụ đúng sẽ mất khoản khớp ngưỡng nếu LLM trích câu không khớp chính xác.

## 4. Kết luận (5 điểm)

> **Nên dùng KG khi câu hỏi phải nối hai nguồn không chung từ vựng**, như "vụ án trong báo → Điều luật → khoản theo khối lượng". Trên 3 câu cross-kb (Q3–Q5), Flat đạt recall 0,33 / 0,33 / 0,40 và judge 1 ở cả ba câu, vì chunk luật không bao giờ vào top-3 cho một câu hỏi chỉ nêu tên người. GraphRAG đạt 1,00 và judge 2 ở cả ba câu. Câu aggregation (Q6) cũng lợi, vì graph liệt kê đủ 5 vụ có MDMA, không bị giới hạn bởi top-k.
>
> **Flat RAG là đủ khi đáp án nằm trong một đoạn văn bản** (Q1 định nghĩa luật, Q2 tên bị cáo trong một bài): cả hai đều đạt 1,00 / 2, còn graph chỉ làm prompt dài ×3,6 (2 518 so với 696 token) và chậm hơn khoảng ×1,5.
>
> Điều kiện cụ thể để đáng dựng KG:
> 1. Dữ liệu có **cấu trúc đều để parse rẻ** (văn bản luật: regex, 0 token) và một **thực thể cầu nối có tên chuẩn** (tội danh).
> 2. Tỉ lệ câu hỏi đa bước hoặc liệt kê đáng kể. Ở đây là 4/6 câu, và recall trung bình tăng từ 0,51 lên 0,89, judge từ 1,50 lên 2,00.
> 3. Có người duy trì pipeline trích xuất, vì lỗi ở đó **im lặng** và phá toàn bộ lợi ích (E1: recall graph rơi về 0,68 khi trích xuất hỏng).
>
> Chi phí tiền không phải rào cản: khoảng $0,006 trả một lần cộng khoảng $0,0002 mỗi câu (ước tính theo token).
>
> **Ontology tự thiết kế so với gợi ý** (cùng provider, mỗi bên chạy một lần): judge trung bình 2,00 so với 1,83; token vào mỗi câu 2 518 so với 5 589 (−55%); Q4 từ 0,67 / 1 lên 1,00 / 2. Recall trung bình thấp hơn (0,89 so với 0,94), nhưng toàn bộ chênh lệch nằm ở Q6, nơi recall đo sai (E4). Mỗi bên mới chạy một lần, nên chênh lệch nhỏ (như judge 1,83 so với 2,00) có thể nằm trong dao động giữa các lần chạy.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
48 passed in 0.10s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = gemini:gemini-3.5-flash-lite | embedding = gemini:gemini-embedding-001
[OK] KG-2 build_graph: 147 node / 512 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 15 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00000. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: **Trần Thanh Tuấn** (vụ mua bán hơn 36kg ma túy, tử hình; đi tới Điều 251 và Điều 255).

*Ghi chú về ảnh:* 3 ảnh được chụp trên graph dựng lại bằng `python bench_kg.py --build`, chạy sau `--check` vì `--check` thay graph bằng bản nhỏ. Graph này có **202 node / 604 cạnh** (`Case` 11, `Person` 45, `Location` 5). Graph trong lần chạy benchmark có 197 node / 600 cạnh: phần luật giống hệt nhau (regex), phần tin tức lệch vài node vì LLM trích xuất không hoàn toàn giống nhau giữa các lần chạy. Tập label (7) và loại quan hệ (8) giống nhau ở cả hai graph và khớp `ONTOLOGY.md`.

## Vấn đề gặp phải (không tính điểm)

> - **OpenRouter 402 `Insufficient credits`** ở lần `--build` đầu: tài khoản chưa nạp credit, nên chuyển sang Gemini.
> - **Gemini 429** (free tier 15 request/phút) làm `--build` crash. Đã tăng `max_retries` của client OpenAI SDK trong `src/llm.py` (`LLM_MAX_RETRIES`, mặc định 10). Hệ quả: thời gian chờ retry bị tính vào cột `seconds` (mục 1).
> - **Giá USD = 0:** `gemini-3.5-flash-lite` không có trong `PRICES_PER_M`. Tôi không tự thêm giá vì không có nguồn giá chính thức; mục 1 so chi phí bằng token.
> - **Lần `--judge` đầu cho graph chỉ 147 node** (lỗi E1). Đã sửa bằng retry trong `extract_cases`, rồi chạy lại. File nộp là của lần chạy lại.
