"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca", "Etomidate"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def fold_accents(text: str) -> str:
    """'ma tuý' / 'ma túy' -> 'ma tuy': drops Vietnamese tone marks so accent-placement variants compare equal."""
    text = unicodedata.normalize("NFD", text).replace("đ", "d").replace("Đ", "D")
    return "".join(ch for ch in text if not unicodedata.combining(ch))

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    # Normalize both sides (NFC first: news text may use decomposed accents), keep a map back to `known`.
    target = normalize(unicodedata.normalize("NFC", name or ""))
    if not target:
        return None
    canonical = {normalize(unicodedata.normalize("NFC", k)): k for k in known}
    if target in canonical:
        return canonical[target]
    # Same words, different tone-mark placement (ma tuý / ma túy).
    folded = {fold_accents(k): original for k, original in canonical.items()}
    target = fold_accents(target)
    if target in folded:
        return folded[target]
    # Close spelling variant only; a wrong link is worse than none.
    close = difflib.get_close_matches(target, list(folded), n=1, cutoff=0.8)
    if not close:
        return None
    # A mention found word-for-word inside a longer name is a broader concept, not a typo:
    # "sử dụng trái phép chất ma túy" (administrative) must not become "tổ chức sử dụng ..." (Điều 255).
    if f" {target} " in f" {close[0]} ":
        return None
    return folded[close[0]]

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Own ontology (report/ONTOLOGY.md): extraction helpers
# ----------------------------------------------------------------------------------------------

# Street / synonym names -> canonical Substance.name. Stored as Substance.aliases.
SUBSTANCE_ALIASES = {
    "MDMA": ["thuốc lắc", "kẹo", "ma túy kẹo"],
    "Methamphetamine": ["ma túy đá", "hồng phiến"],
    "Heroine": ["heroin", "hêrôin"],
    "Ketamine": ["ketamin"],
    "Cocaine": ["cocain"],
    "Etomidate": ["pod chill"],
    "cần sa": ["marijuana", "bồ đà"],
}
STAGES = ["bắt giữ", "khởi tố", "truy tố", "xét xử sơ thẩm", "xét xử phúc thẩm", "khác"]
POINT_START = re.compile(r"^([a-zđ])\)\s*(.+)$", re.MULTILINE)
LAW_AMOUNT = re.compile(r"(?:từ\s+)?([\d.,]+)\s*(gam|kilôgam)\s*(?:đến dưới\s+([\d.,]+)\s*(gam|kilôgam)|trở lên)")
NEWS_AMOUNT = re.compile(r"([\d.,]+)\s*(kg|kilôgam|kilogam|gam|gram|gr|g)(?![a-zà-ỹ])", re.IGNORECASE)

def _number(text: str) -> float:
    """Vietnamese number: '9,6' -> 9.6, '1.000' -> 1000, '0,686' -> 0.686."""
    if "," in text:
        return float(text.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        return float(text.replace(".", ""))
    return float(text)

def parse_grams(amount: str) -> float | None:
    """'hơn 9,6kg' -> 9600.0, 'khoảng 406g' -> 406.0, '5 viên' -> None."""
    match = NEWS_AMOUNT.search(unicodedata.normalize("NFC", amount or ""))
    if not match:
        return None
    try:
        value = _number(match.group(1).strip(".,"))
    except ValueError:
        return None
    return value * 1000 if match.group(2).lower() in ("kg", "kilôgam", "kilogam") else value

def parse_thresholds(clause_text: str) -> list[dict]:
    """Points like 'b) Heroine, ..., MDMA hoặc XLR-11 có khối lượng 100 gam trở lên;' -> one row per substance."""
    rows = []
    for point, text in POINT_START.findall(clause_text):
        match = LAW_AMOUNT.search(text)
        if not match:
            continue
        to_g = lambda value, unit: _number(value) * (1000 if unit == "kilôgam" else 1)
        min_g = to_g(match.group(1), match.group(2))
        max_g = to_g(match.group(3), match.group(4)) if match.group(3) else None
        rows += [{"substance": s, "point": point, "min_g": min_g, "max_g": max_g} for s in find_substances(text)]
    return rows

def person_key(name: str) -> str:
    """'Lê  Minh Thành' / 'lê minh thành' -> 'lê minh thành' (Person MERGE key)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", name or "").strip().lower())

def canonical_substance(name: str) -> str | None:
    """LLM substance name -> canonical name; unknown substances keep their own (trimmed) name."""
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFC", name or "").strip().strip("\"'“”"))
    full = lowered = text.lower()
    for prefix in ("chat ma tuy ", "ma tuy "):  # 'ma túy MDMA' -> 'mdma' (NFC chars and folded chars align 1:1)
        if fold_accents(lowered).startswith(prefix):
            lowered = lowered[len(prefix):]
            break
    if fold_accents(lowered) in ("", "ma tuy", "chat ma tuy", "cac loai"):
        return None
    for canonical, aliases in SUBSTANCE_ALIASES.items():
        if {full, lowered} & {canonical.lower(), *aliases}:   # 'ma túy đá' is itself an alias
            return canonical
    return link_entity(lowered, SUBSTANCES, normalize=lambda s: s.strip().lower()) or text

OWN_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Chỉ lấy vụ việc là CHỦ ĐỀ CỦA BÀI (khớp tiêu đề). Cuối bài có thể dính
đoạn giới thiệu của một bài báo KHÁC (một vụ không liên quan, không được nhắc lại ở thân bài): BỎ QUA đoạn đó.
Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "stage": "giai đoạn tố tụng mới nhất bài nói tới, một trong: {stages}",
  "charges": ["tội danh của vụ, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH; tính cả hành vi đang bị điều tra/bắt giữ"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp (thuốc lắc, viên kẹo = MDMA; ma túy đá = Methamphetamine)",
                  "amount": "TỔNG khối lượng của chất này trong vụ, nguyên văn kèm đơn vị (ví dụ: hơn 9,6kg), chuỗi rỗng nếu không có"}}],
  "people": [{{"name": "họ tên đầy đủ", "aliases": ["biệt danh, tên tài khoản"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 36 tháng tù; chuỗi rỗng nếu chưa xử"}}]
}}]}}
"Sử dụng trái phép chất ma túy" là vi phạm hành chính, KHÔNG phải tội danh trong danh sách: đừng map nó sang tội khác.
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị, khen thưởng...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one article, then every name the LLM wrote is canonicalized in code."""
    prompt = OWN_EXTRACTION_PROMPT.format(
        stages=" | ".join(STAGES), crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    # Gemini sometimes returns an empty/blocked body for drug-related text; a silent [] would drop the whole
    # article from the graph (seen in a --judge run: 19/20 articles lost), so retry and say so on stderr.
    cases = None
    for attempt in range(1, 4):
        raw = llm_fn(prompt)
        try:
            cases = json.loads(raw).get("cases", [])
            break
        except (json.JSONDecodeError, AttributeError):
            print(f"[cảnh báo] {doc.id}: LLM trả JSON không hợp lệ (lần {attempt}/3): {raw[:80]!r}", file=sys.stderr)
    if cases is None:
        return []
    result = []
    for case in cases if isinstance(cases, list) else []:
        people = []
        for p in case.get("people") or []:
            if not (p.get("name") or "").strip():
                continue
            name = re.sub(r"\s+", " ", unicodedata.normalize("NFC", p["name"]).strip())
            aliases = sorted({a.strip() for a in p.get("aliases") or [] if isinstance(a, str) and a.strip()
                              and person_key(a) != person_key(name)})
            people.append({"key": person_key(name), "name": name, "aliases": aliases,
                           "role": p.get("role") or "", "sentence": p.get("sentence") or "",
                           "charge": link_entity(p.get("charge") or "", known_crimes) or ""})
        # Bridge: union of case-level charges and per-person charges, so either one keeps the bridge up.
        charges = {link_entity(c, known_crimes) for c in case.get("charges") or [] if isinstance(c, str)}
        charges |= {p["charge"] for p in people}
        substances: dict[str, dict] = {}
        for s in case.get("substances") or []:
            name = canonical_substance(s.get("name") or "")
            if not name:
                continue
            row = {"name": name, "amount": s.get("amount") or "", "grams": parse_grams(s.get("amount") or "")}
            old = substances.get(name)  # same substance listed twice: keep the larger (total) amount
            if old is None or (row["grams"] or 0) > (old["grams"] or 0):
                substances[name] = row
        stage = case.get("stage") if case.get("stage") in STAGES else "khác"
        result.append({"name": case.get("name") or doc.metadata.get("title", doc.id),
                       "summary": case.get("summary") or "", "date": case.get("date") or "",
                       "location": (case.get("location") or "").strip(), "stage": stage,
                       "charges": sorted(c for c in charges if c), "substances": list(substances.values()),
                       "people": people})
    return result

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- own ontology: writes

    def own_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "id"),
                           ("Substance", "name"), ("Person", "key"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_article(self, article: dict) -> None:
        """Article + clauses; MENTIONS = substance named in the clause, THRESHOLD = quantity band for it."""
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            FOREACH (t IN clause.thresholds | MERGE (sub:Substance {name: t.substance})
                MERGE (cl)-[r:THRESHOLD {point: t.point}]->(sub) SET r.min_g = t.min_g, r.max_g = t.max_g)
            """,
            **article,
        )

    def add_case(self, case: dict, doc: Document, index: int) -> None:
        """One news case. Crime is only MATCHed (never created here), so a mislabelled charge cannot fork the bridge."""
        case_id = f"{doc.id}#{index}"
        self.run(
            """
            MERGE (k:Case {id: $id})
              SET k.name = $name, k.summary = $summary, k.date = $date, k.stage = $stage,
                  k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            """,
            id=case_id, name=case["name"], summary=case["summary"], date=case["date"], stage=case["stage"],
            location=case["location"], doc_id=doc.id, title=doc.metadata.get("title", ""),
        )
        self.run(
            """
            MATCH (k:Case {id: $id})
            UNWIND $charges AS crime
            MATCH (c:Crime {name: crime})
            MERGE (k)-[:CHARGED_WITH]->(c)
            """,
            id=case_id, charges=case["charges"],
        )
        self.run(
            """
            MATCH (k:Case {id: $id})
            UNWIND $substances AS s
            MERGE (sub:Substance {name: s.name})
            MERGE (k)-[r:INVOLVES]->(sub) SET r.amount = s.amount, r.grams = s.grams
            """,
            id=case_id, substances=case["substances"],
        )
        self.run(
            """
            MATCH (k:Case {id: $id})
            UNWIND $people AS p
            MERGE (person:Person {key: p.key})
              ON CREATE SET person.name = p.name
            SET person.aliases = reduce(acc = coalesce(person.aliases, []), a IN p.aliases |
                                        CASE WHEN a IN acc THEN acc ELSE acc + a END)
            MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence
            """,
            id=case_id, people=case["people"],
        )

    def set_substance_aliases(self) -> None:
        self.run(
            "UNWIND $rows AS row MATCH (s:Substance {name: row.name}) SET s.aliases = row.aliases",
            rows=[{"name": name, "aliases": aliases} for name, aliases in SUBSTANCE_ALIASES.items()],
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        if os.getenv("KG_ONTOLOGY") == "hint":
            return self.context_hint(question, doc_ids)
        # Clause edges (MENTIONS/THRESHOLD, hundreds of them) are left out of the 1-hop facts: the legal
        # basis is added below in readable form, only for the articles a case is actually charged under.
        seed_ids, seed_facts = self.seed_facts(question, doc_ids, skip_labels=("Clause",), limit=30)

        # 1. Cases. Primary = a vector hit or the case of a person named in the question -> full legal basis.
        #    Secondary = only shares a substance named in the question (aggregation, Q6) -> summary line only.
        cases = self.run(
            """
            MATCH (k:Case)
            WITH k, elementId(k) IN $ids
                    OR EXISTS { MATCH (p:Person)-[:INVOLVED_IN]->(k) WHERE elementId(p) IN $ids } AS primary
            WHERE primary OR EXISTS { MATCH (k)-[:INVOLVES]->(s:Substance) WHERE elementId(s) IN $ids }
            OPTIONAL MATCH (k)-[i:INVOLVES]->(s:Substance)
            WITH k, primary, collect(s.name + CASE WHEN coalesce(i.amount, '') = '' THEN '' ELSE ' ' + i.amount END) AS subs
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary, k.stage AS stage, k.date AS date,
                   k.doc_id AS doc_id, primary, subs
            ORDER BY primary DESC, k.id
            """,
            ids=seed_ids,
        )
        facts = []
        for k in cases:
            when = ", ".join(x for x in (k["stage"], k["date"]) if x)
            subs = f" Chất: {'; '.join(k['subs'])}." if k["subs"] else ""
            facts.append(f"Vụ việc '{k['name']}' ({when}; nguồn {k['doc_id']}): {k['summary']}{subs}")
        primary_ids = [k["id"] for k in cases if k["primary"]]

        # 2. Bridge: Case -CHARGED_WITH-> Crime <-DEFINES- Article. When the question names a person, follow
        #    that person's own charge (INVOLVED_IN.charge), not every charge of a 126-suspect case.
        pairs = self.run(
            """
            MATCH (k:Case) WHERE elementId(k) IN $case_ids
            OPTIONAL MATCH (p:Person)-[r:INVOLVED_IN]->(k) WHERE elementId(p) IN $ids AND r.charge <> ''
            WITH k, collect(DISTINCT r.charge) AS person_charges
            MATCH (k)-[:CHARGED_WITH]->(c:Crime)<-[:DEFINES]-(a:Article)
            WHERE size(person_charges) = 0 OR c.name IN person_charges
            RETURN DISTINCT elementId(k) AS k, a.id AS a
            """,
            case_ids=primary_ids, ids=seed_ids,
        )
        # THRESHOLD picks the clause the seized amount falls into (Q5: 9600 g MDMA -> Điều 250 khoản 4 điểm b).
        for row in self.run(
            """
            UNWIND $pairs AS pair
            MATCH (k:Case) WHERE elementId(k) = pair.k
            MATCH (a:Article {id: pair.a})-[:HAS_CLAUSE]->(cl:Clause)-[t:THRESHOLD]->(s:Substance)<-[i:INVOLVES]-(k)
            WHERE i.grams IS NOT NULL AND i.grams >= t.min_g AND (t.max_g IS NULL OR i.grams < t.max_g)
            RETURN k.name AS case_name, a.id AS article, a.title AS title, cl.number AS number, cl.text AS text,
                   cl.penalty AS penalty, t.point AS point, s.name AS substance, i.amount AS amount
            ORDER BY a.id, cl.number
            """,
            pairs=pairs,
        ):
            point = re.search(rf"^{row['point']}\)\s*(.+?);?$", row["text"], re.MULTILINE)
            facts.append(f"Áp dụng cho vụ '{row['case_name']}': {row['substance']} {row['amount']} thuộc "
                         f"[{row['article']} - {row['title']}] khoản {row['number']} điểm {row['point']} "
                         f"({point.group(1) if point else ''}) => {row['penalty']}")

        # 3. Penalty ladder (first line of every clause) of each article reached via the bridge, plus articles
        #    named in the question ("Điều 255"). Gives khoản 1 (base) AND the top clause (Q4: tù chung thân).
        numbers = re.findall(r"[Đđ]iều (\d+)", question)
        for row in self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE (a.id IN $articles OR any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' '))
              AND cl.penalty <> ''
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            ORDER BY a.id, cl.number
            """,
            articles=sorted({p["a"] for p in pairs}), numbers=numbers,
        ):
            facts.append(f"[{row['article']} - {row['title']}] khoản {row['number']}: "
                         f"{row['text'].splitlines()[0].split('. ', 1)[-1].rstrip(':')}")

        return list(dict.fromkeys(facts + seed_facts))[:max_facts]

    def context_hint(self, question: str, doc_ids: list[str]) -> list[str]:
        """Baseline for the bonus comparison: LAB_GUIDE Bước 5 rules a-d over the suggested ontology."""
        seed_ids, facts = self.seed_facts(question, doc_ids)
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary
            """,
            ids=seed_ids,
        )
        facts += [f"Vụ việc '{k['name']}': {k['summary']}" for k in cases]
        rows = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $case_ids
              AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            UNION
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' ')
              AND (cl.number = 1 OR EXISTS { MATCH (cl)-[:MENTIONS]->(s:Substance) WHERE s.name IN $subs })
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            """,
            case_ids=[k["id"] for k in cases], numbers=re.findall(r"[Đđ]iều (\d+)", question),
            subs=find_substances(question),
        )
        facts += [f"[{r['article']} - {r['title']}] khoản {r['number']}: {r['text']}"
                  for r in sorted(rows, key=lambda r: (r["article"], r["number"]))]
        return facts

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    if os.getenv("KG_ONTOLOGY") == "hint":  # baseline run for ket_qua_benchmark_kg.hint.txt (bonus evidence)
        graph.suggested_constraints()
        articles = [parse_law_article(d) for d in law_docs]
        for a in articles:
            graph.add_law_article(a)
        crimes = [a["crime"] for a in articles if a["crime"]]
        for d in news_docs:
            for case in extract_news_cases(d, lambda p: llm_fn(p, json_mode=True), crimes):
                graph.add_news_case(case, d)
        return
    # Own ontology (report/ONTOLOGY.md). Law: regex (deterministic). News: LLM -> JSON -> canonicalized in code.
    graph.own_constraints()
    articles = [parse_law_article(d) for d in law_docs]
    for article in articles:
        for clause in article["clauses"]:
            clause["thresholds"] = parse_thresholds(clause["text"])
        graph.add_article(article)
    crimes = [a["crime"] for a in articles if a["crime"]]
    for doc in news_docs:
        for index, case in enumerate(extract_cases(doc, lambda p: llm_fn(p, json_mode=True), crimes)):
            graph.add_case(case, doc, index)
    graph.set_substance_aliases()

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)            # same retrieval as flat RAG
        doc_ids = list(dict.fromkeys(c["metadata"]["doc_id"] for c in chunks if c["metadata"].get("doc_id")))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {fact}" for fact in facts) or "- (không có)",
            chunks="\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1)),
            question=question,
        )
        return self.llm_fn(prompt)
