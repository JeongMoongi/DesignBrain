import pytest
from qdrant_client.models import PointStruct
import ai_engine
import backlog
import clients
import indexing
import search
import config
from fakes import fake_openai


@pytest.mark.parametrize("img,expected", [
    ({"caption": "보스 등장 연출", "section": "패턴 > Powerbomb"}, "석화체 · 보스 등장 연출"),  # 노션 캡션 우선
    ({"caption": "", "section": "패턴 > Powerbomb"}, "석화체 · 패턴 > Powerbomb"),
    ({"caption": "", "section": ""}, "석화체"),
    ({"caption": "", "section": "상세 > 맵 클리어 보상 > 획득 절차 > 보상 활성화"}, "석화체 · 획득 절차 > 보상 활성화"),
    ({"caption": "초록색 : 넉백 3\n붉은색 : 없음", "section": ""}, "석화체 · 초록색 : 넉백 3 붉은색 : 없음"),
    ({"caption": "가" * 100, "section": ""}, "석화체 · " + "가" * 73 + "…"),
])
def test_image_caption(img, expected):
    assert indexing.image_caption(img, "석화체") == expected


def test_chunk_item_name():
    chunk = "문서: 챕터 1 몹\n[데이터베이스: 챕터 1 몹]\n- 항목: [석화체] ➔ 유형='슈퍼아머'"
    assert indexing.chunk_item_name(chunk) == "석화체"
    assert indexing.chunk_item_name("문서: 챕터 1 몹\n# 챕터 1 몹") == ""


def test_name_aliases_include_parts():
    assert search.name_aliases("덩굴 추적자 (VineStalker)") >= {"덩굴추적자", "vinestalker"}
    assert "변종분쇄자" in search.name_aliases("챕터 1 미니보스 ‘변종 분쇄자’")
    assert "디고리" in search.name_aliases("인카운터 미니보스 - 디고리")
    # ' - ' 앞의 분류명은 별칭으로 쓰지 않는다
    assert search.name_aliases("패턴 3 - Meyham (익스포즈)") == {"패턴3-meyham(익스포즈)", "meyham(익스포즈)", "meyham", "익스포즈"}


def test_match_names_prefers_longer_name():
    catalog = {"분쇄자": {("P1", "분쇄자")}, "변종분쇄자": {("P2", "")}, "석화체": {("P1", "석화체")}}
    assert search.match_names("변종 분쇄자 패턴 알려줘", catalog) == [("P2", "")]
    assert search.match_names("석화체랑 분쇄자 비교해줘", catalog) == [("P1", "석화체"), ("P1", "분쇄자")]
    assert search.match_names("영웅 사망", catalog) == []


def point(idx, page_id, title, text, vector, item=None):
    payload = {"page_id": page_id, "title": title, "text": text, "chunk_idx": idx}
    if item:
        payload["item"] = item
    return PointStruct(id=indexing.chunk_point_id(page_id, idx), vector=vector, payload=payload)


def vec(*head):
    return list(head) + [0.0] * (config.EMBEDDING_DIM - len(head))


def test_named_item_comes_first_even_when_dissimilar(local_db):
    local_db.upsert(config.COLLECTION_NAME, points=[
        point(0, "P1", "챕터 1 몹", "- 항목: [석화체] ➔ 긴 패턴 설명", vec(0.0, 1.0), item="석화체"),
        point(1, "P1", "챕터 1 몹", "- 항목: [분쇄자] ➔ 짧은 설명", vec(1.0, 0.0), item="분쇄자"),
        point(0, "P2", "파트너 특성 시스템", "효과: 쿨타임 감소", vec(1.0, 0.1)),
    ])
    results, named_count = search.search_chunks("석화체 패턴 알려줘", vec(1.0, 0.0))
    assert named_count == 1
    assert results[0].payload["item"] == "석화체"
    assert len({p.id for p in results}) == len(results) == 3


def test_answer_uses_images_of_named_item_and_ranked_links(local_db, monkeypatch):
    def with_images(p, images):
        p.payload["images"] = [{"block_id": b, "caption": "도판"} for b in images]
        p.payload["url"] = f"https://notion/{p.payload['page_id']}"
        return p
    local_db.upsert(config.COLLECTION_NAME, points=[
        with_images(point(0, "P1", "챕터 1 몹", "- 항목: [석화체] ➔ 설명", vec(0.0, 1.0), item="석화체"), ["petri"]),
        with_images(point(0, "P2", "파트너 특성 시스템", "효과: 쿨타임 감소", vec(1.0, 0.0)), ["partner"]),
    ])
    monkeypatch.setattr(clients, "get_embedding", lambda q: vec(1.0, 0.0))
    monkeypatch.setattr(backlog, "notes_for_answer", lambda vector, page_ids: [])
    monkeypatch.setattr(clients, "get_openai", lambda: fake_openai("답변\n사용한 문서: 챕터 1 몹 | 파트너 특성 시스템"))

    result = ai_engine.ask_ai_with_history("석화체 알려줘", [])
    # 유사도로 채운 '파트너 특성 시스템'의 도판은 쓰지 않는다
    assert [img["block_id"] for img in result["images"]] == ["petri"]
    # 참고 문서는 검색 순서(이름 일치 먼저)대로
    assert result["AI_답변"].index("[챕터 1 몹]") < result["AI_답변"].index("[파트너 특성 시스템]")


class Doc:
    def __init__(self, id, title, page_id):
        self.id, self.payload = id, {"title": title, "page_id": page_id}


DOCS = [Doc(1, "챕터 1 몹", "P1"), Doc(2, "파트너 특성 시스템", "P2"), Doc(3, "챕터 1 몹", "P1"),
        Doc(4, "챕터 1 미니보스 ‘변종 분쇄자’", "P3")]


def ids(hits):
    return [h.id for h in hits]


def test_used_docs_line_is_removed_and_selects_docs():
    reply, used = ai_engine.split_used_docs("석화체는...\n\n사용한 문서: 챕터 1 몹", DOCS)
    assert reply == "석화체는..."
    assert ids(used) == [1, 3]


@pytest.mark.parametrize("line,expected", [
    ("**사용한 문서:** 챕터1 몹 | 파트너 특성 시스템", [1, 2, 3]),   # 굵게 표시, 띄어쓰기 차이
    ("사용한 문서: 챕터 1 미니보스 ‘변종 분쇄자’", [4]),            # 따옴표로 끝나는 제목
    ("사용한 문서: 없음", []),
    ("사용한 문서: 지어낸 문서", []),                               # 검색 결과에 없는 제목은 무시
])
def test_used_docs_variants(line, expected):
    reply, used = ai_engine.split_used_docs(f"답변\n{line}", DOCS)
    assert reply == "답변"
    assert ids(used) == expected


def test_missing_used_docs_line_falls_back_to_top_document():
    reply, used = ai_engine.split_used_docs("답변만 있음", DOCS)
    assert reply == "답변만 있음"
    assert ids(used) == [1, 3]


def test_whole_document_fits_and_multiple_names_share_slots(local_db):
    # 항목 11개짜리 문서도 통째로 들어가고, 이름이 둘 일치하면 번갈아 채운다
    local_db.upsert(config.COLLECTION_NAME, points=[
        point(i, "P1", "챕터 2 몹", f"- 항목: [몹{i}] ➔ 설명", vec(1.0, i / 100), item=f"몹{i}") for i in range(11)
    ] + [point(i, "P2", "게이트", f"게이트 본문 {i}", vec(1.0, i / 100)) for i in range(11)])

    results, named_count = search.search_chunks("챕터 2 몹 종류 알려줘", vec(1.0))
    assert {p.payload["item"] for p in results[:named_count]} == {f"몹{i}" for i in range(11)}

    results, named_count = search.search_chunks("챕터 2 몹과 게이트", vec(1.0))
    titles = [p.payload["title"] for p in results[:named_count]]
    assert named_count == search.NAME_MATCH_TOTAL
    assert titles.count("챕터 2 몹") == titles.count("게이트") == search.NAME_MATCH_TOTAL // 2


def test_ambiguous_names_are_ignored(local_db):
    local_db.upsert(config.COLLECTION_NAME, points=[
        point(0, f"P{i}", f"미니보스 {i}", "- 항목: [패턴 1] ➔ 설명", vec(1.0), item="패턴 1")
        for i in range(search.MAX_TARGETS_PER_ALIAS + 1)
    ])
    assert "패턴1" not in search._get_name_catalog()


def test_catalog_is_rebuilt_after_delete(local_db):
    local_db.upsert(config.COLLECTION_NAME, points=[point(0, "P1", "석화체", "본문", vec(1.0))])
    assert "석화체" in search._get_name_catalog()
    indexing.delete_document_from_db("P1")
    assert "석화체" not in search._get_name_catalog()
