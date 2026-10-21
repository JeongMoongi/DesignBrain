import pytest
from qdrant_client.models import PointStruct
import agent
import clients
import librarian
import config
import indexing
import search

VECTOR = [1.0] + [0.0] * (config.EMBEDDING_DIM - 1)
REFINED = ("## 개요\n아이템 DB다.\n\n"
           "## 항목: 맹세의 서\n- 각인 A: 협공\n\n"
           "## 항목: 없는 항목\n- 원본에 없는 이름\n\n"
           "## 획득\n- 상자에서 얻는다")


class FakeResponses:
    def __init__(self, text=REFINED, error=None):
        self.text, self.error, self.requests = text, error, []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        usage = type("Usage", (), {"input_tokens": 100, "output_tokens": 50})()
        return type("Response", (), {"output_text": self.text, "usage": usage})()


@pytest.fixture
def originals(local_db, monkeypatch):
    """'아이템' DB 문서 원본 청크 (본문 1개, 항목 1개)"""
    monkeypatch.setattr(clients, "get_embeddings", lambda texts: [VECTOR] * len(texts))
    local_db.upsert(config.COLLECTION_NAME, [
        PointStruct(id=1, vector=VECTOR, payload={"page_id": "P1", "chunk_idx": 0, "title": "아이템", "path": "아이템 및 각인",
                                                  "url": "https://notion/P1", "text": "문서: 아이템\n# 아이템",
                                                  "images": [{"block_id": "doc-img", "caption": "아이템"}]}),
        PointStruct(id=2, vector=VECTOR, payload={"page_id": "P1", "chunk_idx": 1, "title": "아이템", "path": "아이템 및 각인",
                                                  "url": "https://notion/P1", "item": "맹세의 서",
                                                  "text": "문서: 아이템\n- 항목: [맹세의 서] ➔ 각인 A='협공'",
                                                  "images": [{"block_id": "item-img", "caption": "맹세의 서"}]})])
    return local_db


@pytest.fixture
def fake_openai(monkeypatch):
    def install(**kwargs):
        fake = FakeResponses(**kwargs)
        monkeypatch.setattr(clients, "get_openai", lambda: type("OpenAI", (), {"responses": fake})())
        return fake
    return install


def refined_points(db):
    points, _ = db.scroll(config.REFINED_COLLECTION, limit=100, with_payload=True)
    return sorted(points, key=lambda p: p.payload["chunk_idx"])


def test_split_sections_keeps_only_known_item_names():
    sections = librarian.split_sections(REFINED, ["맹세의 서"])
    assert [item for item, _ in sections] == ["", "맹세의 서", "", ""]
    assert sections[1][1] == "## 항목: 맹세의 서\n- 각인 A: 협공"


def test_split_long_section_repeats_heading(monkeypatch):
    monkeypatch.setattr(librarian, "REFINED_CHUNK_CHARS", 30)
    pieces = librarian.split_long_section("## 규칙\n- 첫 번째 규칙 설명\n- 두 번째 규칙 설명\n- 세 번째")
    assert len(pieces) > 1
    assert pieces[0].startswith("## 규칙\n")
    assert all(p.startswith("## 규칙 (이어서)\n") for p in pieces[1:])


def test_refine_document_stores_chunks_like_originals(originals, fake_openai):
    fake = fake_openai()
    assert librarian.refine_document("P1") == 4
    request = fake.requests[0]
    assert request["model"] == config.REFINE_MODEL
    assert "[DB 항목 목록] 맹세의 서" in request["input"] and "- 항목: [맹세의 서]" in request["input"]
    assert "문서: 아이템" not in request["input"]

    points = refined_points(originals)
    assert [p.payload.get("item") for p in points] == [None, "맹세의 서", None, None]
    assert points[1].payload["text"] == "문서: 아이템 (위치: 아이템 및 각인)\n## 항목: 맹세의 서\n- 각인 A: 협공"
    assert points[1].payload["images"] == [{"block_id": "item-img", "caption": "맹세의 서"}]
    assert points[0].payload["images"] == [{"block_id": "doc-img", "caption": "아이템"}]
    assert points[0].payload["sources"] == [{"page_id": "P1", "title": "아이템", "url": "https://notion/P1"}]
    assert points[0].payload["url"] == "https://notion/P1"


def test_refine_document_skips_unchanged_original_and_removes_old_chunks(originals, fake_openai):
    fake = fake_openai()
    librarian.refine_document("P1")
    assert librarian.refine_document("P1") is None
    assert len(fake.requests) == 1

    fake.text = "## 개요\n짧아졌다"
    assert librarian.refine_document("P1", force=True) == 1
    assert len(refined_points(originals)) == 1


def test_refine_document_deletes_refined_when_original_is_gone(originals, fake_openai):
    fake_openai()
    librarian.refine_document("P1")
    indexing._delete_page_points("P1")
    assert refined_points(originals) == []


def test_long_originals_are_refined_in_parts(originals, fake_openai, monkeypatch):
    monkeypatch.setattr(librarian, "REFINE_PART_CHARS", 10)
    fake = fake_openai()
    librarian.refine_document("P1")
    assert len(fake.requests) == 2
    assert "1번째 부분" in fake.requests[0]["input"] and "'## 개요'는 쓰지 말고" in fake.requests[1]["input"]


def test_jobs_are_removed_after_refining(originals, fake_openai):
    fake_openai()
    librarian.enqueue("P1")
    assert librarian.process_jobs() == 1
    assert librarian.pending_jobs() == []
    assert len(refined_points(originals)) == 4


def test_failed_jobs_are_retried_then_dropped(originals, fake_openai):
    fake_openai(error=RuntimeError("boom"))
    librarian.enqueue("P1")
    for attempt in range(1, librarian.JOB_MAX_ATTEMPTS):
        librarian.process_jobs()
        assert librarian.pending_jobs()[0]["attempts"] == attempt
    librarian.process_jobs()
    assert librarian.pending_jobs() == []


def test_job_requested_again_while_refining_is_kept(originals, fake_openai, monkeypatch):
    fake_openai()
    librarian.enqueue("P1")
    original_refine = librarian.refine_document

    def refine_and_request_again(page_id, **kwargs):
        librarian.enqueue(page_id)
        return original_refine(page_id, **kwargs)

    monkeypatch.setattr(librarian, "refine_document", refine_and_request_again)
    librarian.process_jobs()
    assert [j["page_id"] for j in librarian.pending_jobs()] == ["P1"]


def test_agent_reads_refined_documents_and_originals(originals, fake_openai):
    fake_openai()
    librarian.refine_document("P1")
    search.invalidate_name_catalog()
    state = agent.AgentState(None, config.REFINED_COLLECTION)
    refined = agent.tool_read_document(state, "아이템", item="맹세의 서")
    assert refined == "[문서: 아이템 > 맹세의 서]\n## 항목: 맹세의 서\n- 각인 A: 협공"
    original = agent.tool_read_original(state, "아이템", item="맹세의 서")
    assert original == "[원본: 아이템 > 맹세의 서]\n- 항목: [맹세의 서] ➔ 각인 A='협공'"
