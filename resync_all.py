"""인덱스된 노션 문서를 현재 코드로 다시 동기화한다. 파싱/청킹 규칙을 바꾼 뒤 기존 문서에 반영할 때 사용.

    python resync_all.py              # 인덱스된 모든 문서
    python resync_all.py <page_id>... # 지정한 문서만

웹훅과 같은 sync_document_to_db를 거치므로 휴지통 문서는 저장되지 않고 인덱스에서 제거된다.
"""
import logging
import sys
import config
import ai_engine

logger = logging.getLogger("resync_all")


def indexed_page_ids():
    client = ai_engine.get_qdrant()
    page_ids, offset = set(), None
    while True:
        points, offset = client.scroll(ai_engine.COLLECTION_NAME, limit=256, offset=offset, with_payload=["page_id"])
        page_ids.update(p.payload["page_id"] for p in points if p.payload.get("page_id"))
        if offset is None:
            return sorted(page_ids)


def main(page_ids):
    page_ids = page_ids or indexed_page_ids()
    client = ai_engine.get_qdrant()
    before = client.count(ai_engine.COLLECTION_NAME, exact=True).count
    logger.info("재동기화 시작: 문서 %d개 (현재 청크 %d개)", len(page_ids), before)

    skipped, failed = [], []
    for i, page_id in enumerate(page_ids, 1):
        try:
            if not ai_engine.sync_document_to_db(page_id, config.NOTION_INTEGRATION_TOKEN):
                skipped.append(page_id)
        except Exception as e:
            logger.exception("동기화 실패: %s", page_id)
            failed.append((page_id, e))
        logger.info("진행 %d/%d", i, len(page_ids))

    after = client.count(ai_engine.COLLECTION_NAME, exact=True).count
    logger.info("재동기화 완료: 청크 %d개 -> %d개, 저장 안 함(휴지통/빈 문서) %d개, 실패 %d개", before, after, len(skipped), len(failed))
    for page_id in skipped:
        logger.info("  저장 안 함: %s", page_id)
    for page_id, e in failed:
        logger.error("  실패: %s (%s)", page_id, e)
    ai_engine.close_clients()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
