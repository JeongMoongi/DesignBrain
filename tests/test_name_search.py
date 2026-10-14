from qdrant_client.models import PointStruct
import ai_engine
import config


def test_chunk_item_name():
    chunk = "문서: 챕터 1 몹\n[데이터베이스: 챕터 1 몹]\n- 항목: [석화체] ➔ 유형='슈퍼아머'"
    assert ai_engine.chunk_item_name(chunk) == "석화체"
    assert ai_engine.chunk_item_name("문서: 챕터 1 몹\n# 챕터 1 몹") == ""


def test_name_aliases_include_parts():
    assert ai_engine.name_aliases("덩굴 추적자 (VineStalker)") >= {"덩굴추적자", "vinestalker"}
    assert "변종분쇄자" in ai_engine.name_aliases("챕터 1 미니보스 ‘변종 분쇄자’")
    assert "디고리" in ai_engine.name_aliases("인카운터 미니보스 - 디고리")
    # ' - ' 앞의 분류명은 별칭으로 쓰지 않는다
    assert ai_engine.name_aliases("패턴 3 - Meyham (익스포즈)") == {"패턴3-meyham(익스포즈)", "meyham(익스포즈)", "meyham", "익스포즈"}


def test_match_names_prefers_longer_name():
    catalog = {"분쇄자": {("P1", "분쇄자")}, "변종분쇄자": {("P2", "")}, "석화체": {("P1", "석화체")}}
    assert ai_engine.match_names("변종 분쇄자 패턴 알려줘", catalog) == [("P2", "")]
    assert ai_engine.match_names("석화체랑 분쇄자 비교해줘", catalog) == [("P1", "석화체"), ("P1", "분쇄자")]
    assert ai_engine.match_names("영웅 사망", catalog) == []


def point(idx, page_id, title, text, vector, item=None):
    payload = {"page_id": page_id, "title": title, "text": text, "chunk_idx": idx}
    if item:
        payload["item"] = item
    return PointStruct(id=ai_engine.chunk_point_id(page_id, idx), vector=vector, payload=payload)


def vec(*head):
    return list(head) + [0.0] * (config.EMBEDDING_DIM - len(head))


def test_named_item_comes_first_even_when_dissimilar(local_db):
    local_db.upsert(config.COLLECTION_NAME, points=[
        point(0, "P1", "챕터 1 몹", "- 항목: [석화체] ➔ 긴 패턴 설명", vec(0.0, 1.0), item="석화체"),
        point(1, "P1", "챕터 1 몹", "- 항목: [분쇄자] ➔ 짧은 설명", vec(1.0, 0.0), item="분쇄자"),
        point(0, "P2", "파트너 특성 시스템", "효과: 쿨타임 감소", vec(1.0, 0.1)),
    ])
    results = ai_engine.search_chunks("석화체 패턴 알려줘", vec(1.0, 0.0))
    assert results[0].payload["item"] == "석화체"
    assert len({p.id for p in results}) == len(results) == 3


def test_ambiguous_names_are_ignored(local_db):
    local_db.upsert(config.COLLECTION_NAME, points=[
        point(0, f"P{i}", f"미니보스 {i}", "- 항목: [패턴 1] ➔ 설명", vec(1.0), item="패턴 1")
        for i in range(ai_engine.MAX_TARGETS_PER_ALIAS + 1)
    ])
    assert "패턴1" not in ai_engine._get_name_catalog()


def test_catalog_is_rebuilt_after_delete(local_db):
    local_db.upsert(config.COLLECTION_NAME, points=[point(0, "P1", "석화체", "본문", vec(1.0))])
    assert "석화체" in ai_engine._get_name_catalog()
    ai_engine.delete_document_from_db("P1")
    assert "석화체" not in ai_engine._get_name_catalog()
