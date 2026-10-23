"""슬랙의 질문 기록 기능: 답변에 단 👍/👎 리액션을 기록에 반영하고, 매주 질문 기록 요약을 DM으로 보낸다"""
import logging
import threading
from datetime import datetime, timedelta
import backlog
import config
import librarian_report
import qa_log

logger = logging.getLogger(__name__)

report_stop = threading.Event()


def handle_reaction(event, added):
    """reaction_added/removed 이벤트. 봇 답변이 아닌 메세지나 👍/👎가 아닌 리액션은 무시한다."""
    value = qa_log.feedback_of(event.get("reaction", ""))
    item = event.get("item", {})
    if value is None or item.get("type") != "message":
        return
    try:
        if qa_log.set_feedback(item.get("ts", ""), event.get("user", ""), value, added=added):
            logger.info("🗳️ [질문 기록] 평가 %s %s (답변 ts: %s)", value, "추가" if added else "취소", item.get("ts"))
    except Exception as e:
        logger.warning("⚠️ [질문 기록] 평가 반영 실패: %s", e)


def send_weekly_report(client, now=None):
    until = now or datetime.now(backlog.KST)
    since = until - timedelta(days=7)
    text = qa_log.weekly_report(qa_log.entries(since, until), since, until)
    try:
        librarian = librarian_report.weekly_report(since, until)
    except Exception as e:
        # 사서 활동은 덧붙이는 부분이라 실패해도 질문 기록 요약은 보낸다
        logger.warning("⚠️ [사서] 주간 활동 요약 실패: %s", e)
        librarian = ""
    if librarian:
        text += "\n" + librarian
    client.chat_postMessage(channel=config.QA_REPORT_USER_ID, text=text)
    logger.info("📊 [질문 기록] 주간 요약을 보냈습니다.")


def report_loop(client):
    while True:
        wait = qa_log.seconds_until_weekly(config.QA_REPORT_WEEKDAY, config.QA_REPORT_HOUR)
        logger.info("⏰ [질문 기록] 다음 주간 요약까지 %.1f일", wait / 86400)
        if report_stop.wait(wait):
            return
        try:
            send_weekly_report(client)
        except Exception:
            logger.exception("🚨 [질문 기록] 주간 요약 전송 실패")
