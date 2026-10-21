"""슬랙의 백로그 기능: '@Brain 백로그 @담당자 내용' 등록, 매일 리마인더 DM, [해결됨]/[아직] 버튼"""
import logging
import re
import threading
import ai_engine
import backlog
import config
import slack_service

logger = logging.getLogger(__name__)

BACKLOG_USAGE = "형식: `@Brain 백로그 @담당자 내용` (답변 스레드에서 쓰면 그 답변의 기획서와 연결돼요)"
LEADING_MENTION = re.compile(r'^\s*<@[A-Z0-9]+(?:\|[^>]*)?>\s*')

def backlog_request_text(raw_text):
    """'@Brain 백로그 @담당자 내용' 또는 DM의 '백로그 @담당자 내용'이면 '@담당자 내용' 부분을, 아니면 None"""
    text = LEADING_MENTION.sub("", raw_text or "", count=1)
    if not text.startswith(backlog.BACKLOG_KEYWORD):
        return None
    return text[len(backlog.BACKLOG_KEYWORD):].strip()

def docs_answered_in_thread(client, channel_id, thread_ts, before_ts):
    """스레드에서 이 명령 직전의 봇 답변이 참고한 기획서. 답변 메타데이터로 판별하고,
    메타데이터가 없는 예전 답변은 답변 끝의 노션 링크로 판별한다."""
    messages = client.conversations_replies(channel=channel_id, ts=thread_ts, include_all_metadata=True).get("messages", [])
    for msg in reversed(messages):
        if "bot_id" in msg and float(msg.get("ts", 0)) < float(before_ts):
            payload = slack_service.answer_payload(msg)
            page_ids = payload.get("page_ids", []) if payload else backlog.page_ids_in_text(msg.get("text", ""))
            if page_ids:
                return backlog.linked_docs(page_ids)
    return []

def doc_links(docs):
    return ", ".join(f"<{d['url']}|{d['title']}>" if d.get("url") else d["title"] for d in docs)

def register_backlog(client, say, request_text, reporter_id, reply_ts, reporter_name=None, docs=()):
    parsed = backlog.parse_command(request_text)
    if not parsed:
        say(text=f"❌ 백로그를 등록하지 못했어요. 담당자를 @멘션으로 지정해 주세요.\n{BACKLOG_USAGE}", thread_ts=reply_ts)
        return
    assignee_id, content = parsed
    try:
        record = backlog.add(
            reporter_id=reporter_id, reporter_name=slack_service.user_name(client, reporter_id) or reporter_name,
            assignee_id=assignee_id, assignee_name=slack_service.user_name(client, assignee_id),
            content=content, docs=list(docs), source={"reply_ts": reply_ts or ""},
        )
    except Exception as e:
        logger.exception("🚨 [백로그] 등록 실패")
        failure = ai_engine.describe_failure(e)
        say(text=slack_service.failure_message("백로그 등록", failure["reason"], failure["hint"]), thread_ts=reply_ts)
        return

    linked = doc_links(record["docs"]) if record["docs"] else "없음 (내용이 비슷한 질문에 표시돼요)"
    say(text=(
        f"{backlog.BACKLOG_CONFIRM_PREFIX}.\n"
        f"• *담당자:* <@{assignee_id}>\n"
        f"• *관련 기획서:* {linked}\n"
        f"• *답변에 붙는 문구:* 📌 참고 : {record['note']}\n"
        f"담당자가 해결됨으로 처리할 때까지 매일 오전 {config.BACKLOG_REMINDER_HOUR}시에 확인 DM을 보내요."
    ), thread_ts=reply_ts)

def handle_backlog_mention(client, say, event, request_text):
    """채널 멘션/DM으로 들어온 백로그 명령. 스레드 안이면 직전 답변의 기획서와 연결한다."""
    reply_ts = event.get("thread_ts") or event.get("ts")
    docs = []
    if event.get("thread_ts"):
        try:
            docs = docs_answered_in_thread(client, event["channel"], event["thread_ts"], event["ts"])
        except Exception as e:
            logger.warning("⚠️ [백로그] 스레드에서 연결 기획서를 찾지 못했습니다: %s", e)
    register_backlog(client, say, request_text, event.get("user"), reply_ts, docs=docs)

# 리마인더 DM 한 통에 담는 백로그 수와 블록 수 (슬랙 메세지는 블록 50개 제한, 항목당 블록 2개, 사서 제안은 3개)
REMINDER_ITEMS_PER_MESSAGE = 20
MAX_BLOCKS_PER_MESSAGE = 50
reminder_stop = threading.Event()

def reminder_item_blocks(record):
    if record.get("kind") == backlog.PROPOSAL:
        import proposal_slack
        return proposal_slack.item_blocks(record)
    details = [f"등록: {record.get('reporter_name') or '팀원'} · {record['created_at'][:10]}"]
    if record.get("docs"):
        details.append(f"관련 기획서: {doc_links(record['docs'])}")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{record['content']}*\n" + "\n".join(f"• {d}" for d in details)}},
        {"type": "actions", "block_id": f"backlog_{record['backlog_id']}", "elements": [
            {"type": "button", "action_id": "backlog_resolve", "style": "primary", "value": record["backlog_id"],
             "text": {"type": "plain_text", "text": "✅ 해결됨"}},
            {"type": "button", "action_id": "backlog_pending", "value": record["backlog_id"],
             "text": {"type": "plain_text", "text": "⏳ 아직"}},
        ]},
    ]

def reminder_messages(records):
    """담당자 한 명에게 보낼 DM 메세지들 (블록 목록의 목록). 메세지마다 항목 수와 블록 수 제한을 지킨다"""
    messages = [[{"type": "section", "text": {"type": "mrkdwn", "text":
        f"📋 아직 해결되지 않은 백로그가 *{len(records)}건* 있어요. 기획서에 반영됐다면 [✅ 해결됨]을 눌러 주세요."}}]]
    items_in_message = 0
    for record in records:
        blocks = reminder_item_blocks(record)
        if items_in_message >= REMINDER_ITEMS_PER_MESSAGE or len(messages[-1]) + len(blocks) > MAX_BLOCKS_PER_MESSAGE:
            messages.append([])
            items_in_message = 0
        messages[-1].extend(blocks)
        items_in_message += 1
    return messages

def send_backlog_reminders(client):
    for assignee_id, records in backlog.open_by_assignee().items():
        try:
            for blocks in reminder_messages(records):
                client.chat_postMessage(channel=assignee_id, blocks=blocks, text=f"미해결 백로그 {len(records)}건")
            logger.info("📬 [백로그] %s 에게 미해결 %d건 안내", assignee_id, len(records))
        except Exception as e:
            logger.error("🚨 [백로그] %s 리마인더 DM 실패: %s", assignee_id, e)

def reminder_loop(client):
    while True:
        wait = backlog.seconds_until_next(config.BACKLOG_REMINDER_HOUR)
        logger.info("⏰ [백로그] 다음 리마인더까지 %.0f분", wait / 60)
        if reminder_stop.wait(wait):
            return
        try:
            send_backlog_reminders(client)
        except Exception:
            logger.exception("🚨 [백로그] 리마인더 전송 실패")

def replace_item_actions(blocks, backlog_id, status_text):
    """DM 메세지에서 해당 백로그의 버튼을 처리 결과 문구로 바꾼다"""
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": status_text}]}
            if b.get("block_id") == f"backlog_{backlog_id}" else b for b in blocks]

def handle_backlog_button(body, client, resolve):
    user_id = body["user"]["id"]
    backlog_id = body["actions"][0]["value"]
    channel_id = body["channel"]["id"]
    message = body["message"]
    if resolve:
        result = backlog.resolve(backlog_id, user_id)
        if result == "not_assignee":
            client.chat_postEphemeral(channel=channel_id, user=user_id, text="❌ 해결됨 처리는 담당자만 할 수 있어요.")
            return
        status_text = {"resolved": "✅ 해결됨으로 처리했어요. 이제 답변에 표시되지 않아요.",
                       "already": "✅ 이미 해결됨으로 처리된 백로그예요.",
                       "not_found": "⚠️ 삭제되었거나 찾을 수 없는 백로그예요."}[result]
    else:
        status_text = f"⏳ 내일 오전 {config.BACKLOG_REMINDER_HOUR}시에 다시 안내할게요."
    client.chat_update(channel=channel_id, ts=message["ts"], text=message.get("text", ""),
                       blocks=replace_item_actions(message.get("blocks", []), backlog_id, status_text))
