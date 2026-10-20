import pytest
import config
import doc_diagnosis
import notion_parser as np
import search
from test_parser import rt, text_block


@pytest.fixture
def fake_notion(monkeypatch, no_network):
    tree = {}
    monkeypatch.setattr(np, "fetch_block_children", lambda block_id, token: tree.get(block_id, []))
    return tree


def test_parser_outlines_unreadable_parts(monkeypatch, fake_notion):
    # 뷰는 쿼리가 실패(None)하고, 공유되지 않은 링크는 조회가 안 된다
    monkeypatch.setattr(np, "query_notion_database", lambda db_id, token: None)
    monkeypatch.setattr(np, "link_reference", lambda link, token: None)
    fake_notion["page"] = [
        text_block("heading_1", "리더"),
        {"id": "v1", "type": "child_database", "has_children": False, "child_database": {"title": ""}},
        text_block("heading_1", "개인 물품"),
        text_block("heading_1", "상세"),
        text_block("paragraph", "본문"),
        {"id": "l1", "type": "link_to_page", "has_children": False, "link_to_page": {"type": "page_id", "page_id": "x"}},
    ]
    outline = []
    np.extract_blocks_recursive("page", "tok", [], [], outline=outline)
    assert outline == [("heading", 0, "리더"), ("linked_view", "리더 > Untitled"), ("heading", 0, "개인 물품"),
                       ("heading", 0, "상세"), ("content",), ("unshared_link",)]


def test_unshared_relations_are_counted(monkeypatch, fake_notion):
    monkeypatch.setattr(np, "query_notion_database", lambda db_id, token: [{"id": "r1", "properties": {
        "이름": {"type": "title", "title": rt("맹세의 서")},
        "각인": {"type": "relation", "relation": [{"id": "private1"}, {"id": "private2"}]}}}])
    monkeypatch.setattr(np, "fetch_page", lambda page_id, token: None)
    outline = []
    np.fetch_database_rows("db", "아이템 목록", "tok", [], outline)
    assert outline == [("unshared_relation", "아이템 목록", 2)]


@pytest.mark.parametrize("outline,expected", [
    ([("heading", 0, "A"), ("heading", 0, "B"), ("content",)], ["A"]),
    # 하위 제목에 본문이 있으면 상위 제목은 비어 있지 않다. 하위 제목이 비면 그것만
    ([("heading", 0, "A"), ("heading", 1, "A-1"), ("content",), ("heading", 1, "A-2")], ["A-2"]),
    # 뷰만 있는 제목은 뷰로 알리므로 빼고, 문서 끝의 제목은 비어 있다
    ([("heading", 0, "A"), ("linked_view", "A > Untitled"), ("heading", 0, "끝")], ["끝"]),
    ([("heading", 0, "A"), ("unshared_link",), ("heading", 0, "B"), ("content",)], ["A"]),
])
def test_empty_headings(outline, expected):
    assert doc_diagnosis.empty_headings(outline) == expected


def doc(**kwargs):
    return {"page_id": "P1", "title": "전투 요소", "url": "https://notion/P1", "path": ["영웅 목록", "디고리"],
            "full_text": "# 전투 요소\n" + "본문" * 100, "outline": [], **kwargs}


def test_diagnose_reports_facts():
    documents = {"디고리 > 전투 요소": {"page_ids": ["P1"], "items": [], "path": "영웅 목록 > 디고리"},
                 "카일린 > 전투 요소": {"page_ids": ["P2"], "items": [], "path": "영웅 목록 > 카일린"},
                 "전투 요소 정리": {"page_ids": ["P3"], "items": [], "path": ""}}
    findings = doc_diagnosis.diagnose(doc(full_text="# 전투 요소\n짧음", outline=[
        ("heading", 0, "리더"), ("linked_view", "리더 > Untitled"), ("unshared_link",), ("unshared_link",),
        ("unshared_relation", "스킬", 3)]), documents)
    assert {f["kind"]: (f["count"], f["details"]) for f in findings} == {
        "linked_view": (1, ["리더 > Untitled"]),
        "short_body": (1, ["2자"]),
        "unshared_link": (2, ["2개"]),
        "unshared_relation": (3, ["스킬 (3개)"]),
        "same_title": (1, ["영웅 목록 > 카일린"]),
    }
    report = doc_diagnosis.format_report(doc(), findings)
    assert report.startswith("📋 *문서 진단* : <https://notion/P1|전투 요소>")
    assert "• 읽을 수 없는 DB 뷰 (1) : 리더 > Untitled" in report


def test_clean_document_has_no_findings():
    assert doc_diagnosis.diagnose(doc(), {"전투 요소": {"page_ids": ["P1"], "items": [], "path": ""}}) == []


def test_run_saves_latest_and_notifies_only_with_findings(local_db, monkeypatch):
    sent = []
    monkeypatch.setattr(doc_diagnosis, "notify", sent.append)
    monkeypatch.setattr(search, "get_documents", lambda: {})
    doc_diagnosis.run(doc(page_id="11111111-1111-1111-1111-111111111111", full_text="# 전투 요소"), notify_owner=True)
    doc_diagnosis.run(doc(page_id="22222222-2222-2222-2222-222222222222"), notify_owner=True)
    doc_diagnosis.run(doc(page_id="33333333-3333-3333-3333-333333333333", full_text="# 전투 요소"), notify_owner=False)
    assert len(sent) == 1 and "본문이 거의 없음" in sent[0]
    saved = {p.id: p.payload for p in local_db.scroll(config.DIAGNOSIS_COLLECTION, limit=10)[0]}
    assert len(saved) == 3
    assert saved["22222222-2222-2222-2222-222222222222"]["findings"] == []
    assert saved["11111111-1111-1111-1111-111111111111"]["path"] == "영웅 목록 > 디고리"

    doc_diagnosis.delete("11111111-1111-1111-1111-111111111111")
    assert len(local_db.scroll(config.DIAGNOSIS_COLLECTION, limit=10)[0]) == 2


def test_diagnosis_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(search, "get_documents", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert doc_diagnosis.run(doc()) == []


def test_report_groups_by_kind():
    import diagnosis_report
    rows = [{"title": "카일린", "url": "u1", "path": "영웅 목록", "findings": [
                {"kind": "empty_heading", "count": 2, "details": ["리더", "파트너"]},
                {"kind": "linked_view", "count": 1, "details": ["리더 > Untitled"]}]},
            {"title": "아이템", "url": "u2", "path": "", "findings": []}]
    report = diagnosis_report.build_report(rows)
    assert "진단한 문서 2개 중 1개에서 발견" in report
    assert "## 읽을 수 없는 DB 뷰 — 문서 1개, 총 1건" in report
    assert "- [카일린](u1) (영웅 목록): 리더, 파트너" in report
    # 진단 종류 순서(KINDS)대로 나온다
    assert report.index("DB 뷰") < report.index("비어 있는 제목")
