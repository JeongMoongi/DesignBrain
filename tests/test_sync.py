import threading
import time
import pytest
import ai_engine
import notion_parser as np


@pytest.fixture
def notion_page(monkeypatch, local_db, no_network):
    """가짜 노션 페이지 하나. state를 바꿔 휴지통 이동/조회 실패를 흉내 낸다."""
    state = {"trashed": False, "fail": False, "embed_delay": 0.0}

    def fetch_page(page_id, token):
        if state["fail"]:
            return None
        return {"in_trash": state["trashed"], "archived": False, "url": "https://notion/p",
                "properties": {"n": {"type": "title", "title": [{"plain_text": "문서"}]}}}

    def extract(page_id, token, out, images, *args, **kwargs):
        out.extend(["본문 한 줄", "본문 두 줄"])
        images.append({"block_id": "img1", "caption": "도판"})

    def embed(texts):
        time.sleep(state["embed_delay"])
        return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr(np, "fetch_page", fetch_page)
    monkeypatch.setattr(np, "extract_blocks_recursive", extract)
    monkeypatch.setattr(ai_engine, "get_embeddings", embed)
    return state


def count(client):
    return client.count(ai_engine.COLLECTION_NAME, exact=True).count


def test_sync_stores_chunks_with_image_cache(notion_page, local_db):
    assert ai_engine.sync_document_to_db("P", "tok") is True
    points = local_db.scroll(ai_engine.COLLECTION_NAME, limit=10)[0]
    assert len(points) == 1
    assert points[0].payload["images"] == [{"block_id": "img1", "caption": "도판"}]


def test_resync_overwrites_instead_of_duplicating(notion_page, local_db):
    ai_engine.sync_document_to_db("P", "tok")
    ai_engine.sync_document_to_db("P", "tok")
    assert count(local_db) == 1


def test_trashed_page_is_removed_not_indexed(notion_page, local_db):
    ai_engine.sync_document_to_db("P", "tok")
    notion_page["trashed"] = True
    assert ai_engine.sync_document_to_db("P", "tok") is False
    assert count(local_db) == 0


def test_page_trashed_during_sync_is_not_stored(notion_page, local_db, monkeypatch):
    monkeypatch.setattr(np, "is_page_trashed", lambda page_id, token: True)
    assert ai_engine.sync_document_to_db("P", "tok") is False
    assert count(local_db) == 0


def test_failed_recheck_keeps_existing_chunks(notion_page, local_db, monkeypatch):
    ai_engine.sync_document_to_db("P", "tok")

    def check_fails(page_id, token):
        raise RuntimeError("check failed")

    monkeypatch.setattr(np, "is_page_trashed", check_fails)
    with pytest.raises(RuntimeError):
        ai_engine.sync_document_to_db("P", "tok")
    assert count(local_db) == 1


def test_delete_waits_for_running_sync(notion_page, local_db):
    notion_page["embed_delay"] = 0.8
    worker = threading.Thread(target=ai_engine.sync_document_to_db, args=("P", "tok"))
    worker.start()
    time.sleep(0.2)
    notion_page["trashed"] = True  # 동기화 도중 휴지통 이동 + 삭제 웹훅 도착
    started = time.time()
    ai_engine.delete_document_from_db("P")
    waited = time.time() - started
    worker.join()
    assert waited > 0.3
    assert count(local_db) == 0


def test_delete_removes_only_that_page(notion_page, local_db):
    ai_engine.sync_document_to_db("A", "tok")
    ai_engine.sync_document_to_db("B", "tok")
    ai_engine.delete_document_from_db("A")
    remaining = {p.payload["page_id"] for p in local_db.scroll(ai_engine.COLLECTION_NAME, limit=10)[0]}
    assert remaining == {"B"}
