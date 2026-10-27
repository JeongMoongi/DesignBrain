"""한 줄 요약이 생기기 전에 만든 사서 제안(처리 대기·보류)에 한 줄 요약과 '왜 문제인지' 설명을 채운다.

    python backfill_summaries.py --dry-run --limit 4   # 몇 건만 만들어 보여주기 (저장·슬랙 수정 없음)
    python backfill_summaries.py                       # 전부 변환

문서를 다시 탐색하지 않고 저장된 제목·문제·사서 제안만 읽어 다시 쓴다. 사서 제안(노션에 붙일 내용)은 바꾸지 않는다.
예전 설명은 problem_before에 남긴다. 처리 대기 중이고 대화로 고치는 중이 아닌 제안은 원래 슬랙 메세지도 새 형식으로 바꾼다
(보류·대화 중인 메세지는 버튼 자리에 상태 문구가 있어 그대로 두고, 리마인더부터 새 형식으로 보인다).
"""
import argparse
import io
import logging
import sys
import backlog
import clients
import config
import librarian_report
import proposal_slack
import review

logger = logging.getLogger("backfill_summaries")

TASK = "요약 변환"
PROMPT = (
    "너는 게임 기획팀의 기획서 정리 담당 '사서'다. 예전에 낸 [제안]을 기획자가 한눈에 이해하도록 다시 요약한다. "
    "[제목], [예전 설명], [사서 제안]에 있는 내용만 쓰고, 없는 사실을 더하지 마라.\n\n"
    "- category: 문서끼리 어긋나면 \"conflict\", 정의가 비어 있으면 \"missing\", 흩어진 정보를 새 문서로 모으는 것이면 \"topic\"\n"
    + review.SUMMARY_RULES +
    '\nJSON만 출력한다: {"category": "...", "summary": "...", "problem": "..."}'
)


def pending_without_summary():
    records, offset = [], None
    while True:
        points, offset = clients.get_qdrant().scroll(collection_name=config.BACKLOG_COLLECTION, limit=256, offset=offset,
                                                     with_payload=True)
        records += [p.payload for p in points if p.payload.get("kind") == backlog.PROPOSAL
                    and p.payload.get("status") in backlog.UNSETTLED and not p.payload.get("summary")]
        if offset is None:
            return sorted(records, key=lambda r: r["created_at"])


def rewrite(record):
    """{"category", "summary", "problem"} (만들지 못하면 None)"""
    response = clients.get_openai().responses.create(
        model=config.REFINE_MODEL, instructions=PROMPT, reasoning={"effort": "low"},
        input=f"[제목] {record['content']}\n[예전 설명] {record.get('problem', '')}\n[사서 제안]\n{record['proposal'][:3000]}")
    librarian_report.record_usage(TASK, record["content"], *librarian_report.usage_tokens([response.usage]))
    data = review.parse_json(response.output_text or "")
    summary = review.one_line(data.get("summary"))
    if not summary:
        return None
    category = data.get("category") if data.get("category") in review.CATEGORIES else ""
    return {"category": category, "summary": summary, "problem": " ".join(str(data.get("problem") or "").split())}


def main(dry_run: bool, limit: int):
    records = pending_without_summary()[:limit or None]
    logger.info("한 줄 요약이 없는 사서 제안 %d건%s", len(records), " (미리보기)" if dry_run else "")
    slack = None if dry_run else proposal_slack.web_client()
    changed, slack_updated, failed = 0, 0, []
    for i, record in enumerate(records, 1):
        try:
            fields = rewrite(record)
        except Exception as e:
            logger.exception("변환 실패: %s", record["content"])
            failed.append((record["content"], e))
            continue
        if fields is None:
            failed.append((record["content"], "요약을 만들지 못함"))
            continue
        print(f"\n[{i}/{len(records)}] {record['content']} ({record['status']})\n"
              f"  {proposal_slack.headline({**record, **fields})}\n  {fields['problem']}", flush=True)
        if dry_run:
            continue
        if not fields["problem"]:
            fields.pop("problem")
        backlog.update(record["backlog_id"], problem_before=record.get("problem", ""), **fields)
        record.update(fields)
        changed += 1
        if slack and record["status"] == backlog.OPEN and not record.get("chat_ts") and record.get("slack_ts"):
            try:
                slack.chat_update(channel=record["slack_channel"], ts=record["slack_ts"], text=proposal_slack.headline(record),
                                  blocks=proposal_slack.item_blocks(record))
                slack_updated += 1
            except Exception as e:
                logger.warning("슬랙 메세지를 바꾸지 못했습니다 (%s): %s", record["content"], e)
    logger.info("완료: 변환 %d건, 슬랙 메세지 수정 %d건, 실패 %d건", changed, slack_updated, len(failed))
    for content, e in failed:
        logger.warning("  실패: %s (%s)", content, e)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="만들어 보여주기만 하고 저장·슬랙 수정은 하지 않는다")
    parser.add_argument("--limit", type=int, default=0, help="앞에서부터 이 개수만 (0이면 전부)")
    args = parser.parse_args()
    main(args.dry_run, args.limit)
