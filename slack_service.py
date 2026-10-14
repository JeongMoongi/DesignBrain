import logging
import re
import threading
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import ai_engine
import backlog
import config
import notion_parser

logger = logging.getLogger(__name__)

slack_app = None
handler = None

def format_markdown_for_slack(text: str) -> str:
    if not text: return ""
    # 이미지는 링크 변환보다 먼저 제거해야 `![..](..)`가 링크로 바뀌지 않음
    text = re.sub(r'!\[[^\]]*\]\(https?://[^\s)]+\)', '', text)
    text = re.sub(r'\[([^\]]*)\]\((https?://[^\s)]+)\)', r'<\2|🔗 \1>', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)
    text = text.replace("- 참고 문서 :", "• *참고 문서:*")
    text = text.replace("- 참고 문서:", "• *참고 문서:*")
    return text.strip()

# 답변 메세지의 메타데이터에 남기는 정보 (사용자에게는 보이지 않는다)
# - image_block_ids: 첨부한 노션 이미지. 같은 스레드에서 같은 도판을 다시 보내지 않는다 (파일명에 넣으면 슬랙이 지운다)
# - page_ids: 답변에 사용한 기획서. 이 답변 스레드에서 등록한 백로그를 그 기획서와 연결한다
ANSWER_EVENT_TYPE = "brain_answer"
IMAGES_PER_ANSWER = 2

def answer_metadata(page_ids, image_block_ids):
    return {"event_type": ANSWER_EVENT_TYPE, "event_payload": {"page_ids": page_ids, "image_block_ids": image_block_ids}}

def answer_payload(message):
    """봇 답변 메세지의 메타데이터. 이 기능 이전의 답변이나 다른 메세지면 None"""
    metadata = message.get("metadata") or {}
    return metadata.get("event_payload") if metadata.get("event_type") == ANSWER_EVENT_TYPE else None

def shared_image_ids(message) -> list:
    return (answer_payload(message) or {}).get("image_block_ids", [])

def collect_images(candidates, shared_block_ids, limit):
    """검색 순서대로 스레드에 아직 공유되지 않은 이미지를 최대 limit장 수집.
    캐시에는 만료되는 URL이 없으므로 블록을 하나씩 조회해 새 URL을 받는다."""
    token = config.NOTION_INTEGRATION_TOKEN
    if not token:
        return []

    collected = []
    for candidate in candidates:
        block_id = candidate.get("block_id")
        if not block_id or block_id in shared_block_ids:
            continue
        try:
            img = notion_parser.fetch_image_by_block_id(block_id, token)
        except Exception as e:
            logger.warning("⚠️ [Notion] 이미지 블록 조회 실패 (block_id: %s): %s", block_id, e)
            continue
        if not img:
            continue
        # 노션 블록에는 캡션이 비어 있는 경우가 많으므로 동기화 때 만든 설명을 쓴다
        collected.append({**img, "caption": candidate.get("caption") or img.get("caption") or "기획 도판"})
        if len(collected) >= limit:
            break
    return collected

def failure_message(stage, reason, hint):
    return f"❌ 답변을 만들지 못했어요.\n• *실패 단계:* {stage}\n• *원인:* {reason}\n{hint}"

def thread_say(client, channel_id):
    """Bolt의 say 대신 쓴다. Bolt는 DM 스레드를 AI 어시스턴트 스레드로 보고, 보낼 때마다 스레드 첫 봇 답변의
    메타데이터를 어시스턴트 정보로 읽다가 답변 메타데이터(도판 기록)를 만나면 KeyError로 실패한다."""
    def say(text, thread_ts=None):
        return client.chat_postMessage(channel=channel_id, text=text, thread_ts=thread_ts)
    return say

def event_thread_ts(event):
    """스레드 하나를 하나의 대화로 다룬다. 스레드 안의 메세지면 부모 스레드, 아니면 그 메세지로 새 스레드를 시작한다.
    (스레드 답장을 새 스레드로 잡으면 대화 기록이 비어 후속 질문이 맥락 없이 검색된다)"""
    return event.get("thread_ts") or event.get("ts")

def process_chat_and_reply(client, say, channel_id, thread_ts, message_ts, raw_text):
    """어떤 단계에서 실패하더라도 사용자가 '분석 중' 메세지만 보고 기다리지 않도록 실패 원인을 스레드에 안내한다"""
    try:
        _process_chat_and_reply(client, say, channel_id, thread_ts, message_ts, raw_text)
    except Exception as e:
        logger.exception("🚨 [Slack] 메세지 처리 실패")
        failure = ai_engine.describe_failure(e)
        try:
            say(text=failure_message("메세지 처리", failure["reason"], failure["hint"]), thread_ts=thread_ts)
        except Exception:
            logger.exception("🚨 [Slack] 실패 안내 메세지 전송도 실패")

def _process_chat_and_reply(client, say, channel_id, thread_ts, message_ts, raw_text):
    cleaned_query = re.sub(r'<@.*?>', '', raw_text).strip()

    chat_history = []
    shared_block_ids = set()

    try:
        replies = client.conversations_replies(channel=channel_id, ts=thread_ts, include_all_metadata=True)
        messages = replies.get("messages", [])

        for msg in messages:
            shared_block_ids.update(shared_image_ids(msg))
            # 대화 기록은 이 질문 이전까지만 (질문 자신과, 처리 중에 뒤이어 온 메세지는 뺀다)
            if float(msg.get("ts", 0)) >= float(message_ts):
                continue

            msg_text = re.sub(r'<@.*?>', '', msg.get("text", "")).strip()
            if not msg_text or "기획서를 읽고 분석 중입니다" in msg_text:
                continue
            # 백로그 명령/등록 안내는 질문 맥락이 아니므로 대화 기록에서 뺀다
            if msg_text.startswith(BACKLOG_KEYWORD) or msg_text.startswith(BACKLOG_CONFIRM_PREFIX):
                continue

            role = "user" if "bot_id" not in msg else "assistant"
            chat_history.append({"role": role, "content": msg_text})
    except Exception as e:
        logger.info("스레드 히스토리 수집 생략 (DM 또는 단발 메세지): %s", e)

    # 내용 없이 멘션만 하면 이 스레드의 마지막 질문을 다시 묻는 것으로 본다
    final_query = cleaned_query or next((h["content"] for h in reversed(chat_history) if h["role"] == "user"), "")

    if not final_query:
        say(text="네, 기획자님! 무엇이 궁금하신가요?", thread_ts=thread_ts)
        return

    # '분석 중' 메세지를 답변(또는 실패 안내)으로 바꿔, 스레드에 답변당 메세지 하나만 남긴다
    placeholder = say(text=f"🤔 기획서를 읽고 분석 중입니다...", thread_ts=thread_ts)
    placeholder_ts = placeholder["ts"]

    res = ai_engine.ask_ai_with_history(final_query, chat_history)

    if res.get("status") == "error":
        client.chat_update(channel=channel_id, ts=placeholder_ts,
                           text=failure_message(res["stage"], res["reason"], res["hint"]))
        return

    formatted_answer = format_markdown_for_slack(res.get("AI_답변", ""))
    # 노션 파일 URL은 만료되므로 답변할 때마다 노션에서 새로 받아 슬랙에 올린다
    image_objs = collect_images(res.get("images", []), shared_block_ids, limit=IMAGES_PER_ANSWER)
    file_ids, block_ids = upload_images(client, image_objs)
    page_ids = res.get("page_ids", [])
    try:
        client.chat_update(channel=channel_id, ts=placeholder_ts, text=formatted_answer,
                           file_ids=file_ids or None, metadata=answer_metadata(page_ids, block_ids))
    except Exception as e:
        if not file_ids:
            raise
        # 도판 때문에 실패했더라도 답변은 전달한다
        logger.error("🚨 [Slack] 도판을 붙인 답변 전송 실패, 답변만 보냅니다: %s", e)
        client.chat_update(channel=channel_id, ts=placeholder_ts, text=formatted_answer,
                           metadata=answer_metadata(page_ids, []))

def upload_images(client, image_objs):
    """도판을 채널에 공유하지 않고 올려 두기만 한다 (답변 메세지에 붙인다). 올린 파일 ID와 노션 block_id 목록을 돌려준다."""
    file_ids, block_ids = [], []
    for img in image_objs:
        caption = img.get("caption", "기획 도판")
        try:
            img_res = requests.get(img.get("url"), timeout=15)
            if img_res.status_code != 200:
                logger.warning("⚠️ [Notion] 도판 다운로드 실패 (HTTP %s, block_id: %s)", img_res.status_code, img.get("block_id"))
                continue
            uploaded = client.files_upload_v2(file=img_res.content, filename=f"{caption}.jpg", title=f"📸 {caption}")
            file_ids.append(uploaded["file"]["id"])
            block_ids.append(img.get("block_id"))
        except Exception as img_err:
            logger.error("🚨 [Slack] 이미지 파일 업로드 실패: %s", img_err)
    if file_ids:
        logger.info("📤 [Slack] 신규 연관 기획 도판 %d장 첨부", len(file_ids))
    return file_ids, block_ids


# --- 백로그 ---
BACKLOG_KEYWORD = "백로그"
BACKLOG_CONFIRM_PREFIX = "📝 백로그를 등록했어요"
BACKLOG_USAGE = "형식: `@Brain 백로그 @담당자 내용` (답변 스레드에서 쓰면 그 답변의 기획서와 연결돼요)"
LEADING_MENTION = re.compile(r'^\s*<@[A-Z0-9]+(?:\|[^>]*)?>\s*')
_user_names = {}

def backlog_request_text(raw_text):
    """'@Brain 백로그 @담당자 내용' 또는 DM의 '백로그 @담당자 내용'이면 '@담당자 내용' 부분을, 아니면 None"""
    text = LEADING_MENTION.sub("", raw_text or "", count=1)
    if not text.startswith(BACKLOG_KEYWORD):
        return None
    return text[len(BACKLOG_KEYWORD):].strip()

def user_name(client, user_id):
    """슬랙 표시 이름 (users:read 권한이 없거나 실패하면 None). 답변 문구에 멘션을 쓰면 매번 알림이 가므로 이름을 쓴다."""
    if user_id not in _user_names:
        try:
            profile = client.users_info(user=user_id)["user"]
            _user_names[user_id] = profile.get("profile", {}).get("display_name") or profile.get("real_name")
        except Exception as e:
            logger.warning("⚠️ [Slack] 사용자 이름 조회 실패 (%s): %s", user_id, e)
            return None
    return _user_names[user_id]

def docs_answered_in_thread(client, channel_id, thread_ts, before_ts):
    """스레드에서 이 명령 직전의 봇 답변이 참고한 기획서. 답변 메타데이터로 판별하고,
    메타데이터가 없는 예전 답변은 답변 끝의 노션 링크로 판별한다."""
    messages = client.conversations_replies(channel=channel_id, ts=thread_ts, include_all_metadata=True).get("messages", [])
    for msg in reversed(messages):
        if "bot_id" in msg and float(msg.get("ts", 0)) < float(before_ts):
            payload = answer_payload(msg)
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
            reporter_id=reporter_id, reporter_name=user_name(client, reporter_id) or reporter_name,
            assignee_id=assignee_id, assignee_name=user_name(client, assignee_id),
            content=content, docs=list(docs), source={"reply_ts": reply_ts or ""},
        )
    except Exception as e:
        logger.exception("🚨 [백로그] 등록 실패")
        failure = ai_engine.describe_failure(e)
        say(text=failure_message("백로그 등록", failure["reason"], failure["hint"]), thread_ts=reply_ts)
        return

    linked = doc_links(record["docs"]) if record["docs"] else "없음 (내용이 비슷한 질문에 표시돼요)"
    say(text=(
        f"{BACKLOG_CONFIRM_PREFIX}.\n"
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

# 리마인더 DM 한 통에 담는 백로그 수 (슬랙 메세지는 블록 50개 제한, 항목당 블록 2개)
REMINDER_ITEMS_PER_MESSAGE = 20
_reminder_stop = threading.Event()

def reminder_item_blocks(record):
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
    """담당자 한 명에게 보낼 DM 메세지들 (블록 목록의 목록)"""
    messages = []
    for start in range(0, len(records), REMINDER_ITEMS_PER_MESSAGE):
        part = records[start:start + REMINDER_ITEMS_PER_MESSAGE]
        blocks = []
        if start == 0:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                f"📋 아직 해결되지 않은 백로그가 *{len(records)}건* 있어요. 기획서에 반영됐다면 [✅ 해결됨]을 눌러 주세요."}})
        for record in part:
            blocks.extend(reminder_item_blocks(record))
        messages.append(blocks)
    return messages

def send_backlog_reminders(client):
    for assignee_id, records in backlog.open_by_assignee().items():
        try:
            for blocks in reminder_messages(records):
                client.chat_postMessage(channel=assignee_id, blocks=blocks, text=f"미해결 백로그 {len(records)}건")
            logger.info("📬 [백로그] %s 에게 미해결 %d건 안내", assignee_id, len(records))
        except Exception as e:
            logger.error("🚨 [백로그] %s 리마인더 DM 실패: %s", assignee_id, e)

def _reminder_loop(client):
    while True:
        wait = backlog.seconds_until_next(config.BACKLOG_REMINDER_HOUR)
        logger.info("⏰ [백로그] 다음 리마인더까지 %.0f분", wait / 60)
        if _reminder_stop.wait(wait):
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


def init_slack():
    global slack_app, handler
    
    bot_token = config.SLACK_BOT_TOKEN
    app_token = config.SLACK_APP_TOKEN
    
    if not bot_token or not app_token:
        logger.warning("⚠️ [Slack] 토큰 미설정으로 봇을 시작하지 않습니다.")
        return None

    slack_app = App(token=bot_token)

    # 1. 앱 홈 탭 오픈 이벤트 핸들러 (DM 수신 연결용)
    @slack_app.event("app_home_opened")
    def handle_app_home_opened_events(body, logger):
        logger.info("📱 [Slack] 앱 홈/DM 탭이 열렸습니다.")

    # 2. 채널 내 @봇 멘션 처리
    @slack_app.event("app_mention")
    def handle_app_mention_events(body, client):
        event = body.get("event", {})
        channel_id = event.get("channel")
        say = thread_say(client, channel_id)
        thread_ts = event_thread_ts(event)
        text = event.get("text", "")

        logger.info("📢 [채널 멘션 수신] %s", text)
        request_text = backlog_request_text(text)
        if request_text is not None:
            handle_backlog_mention(client, say, event, request_text)
            return
        process_chat_and_reply(client, say, channel_id, thread_ts, event.get("ts"), text)

    # 3. 1:1 DM 메세지 처리
    @slack_app.event("message")
    def handle_message_events(body, client):
        event = body.get("event", {})
        channel_type = event.get("channel_type")
        
        # 봇 자신의 메세지가 아니고, 순수 DM(im)인 경우만 응답
        if channel_type == "im" and not event.get("bot_id") and not event.get("subtype"):
            user_text = event.get("text", "")
            user_id = event.get("user")
            channel_id = event.get("channel")
            thread_ts = event_thread_ts(event)
            say = thread_say(client, channel_id)

            logger.info("💬 [DM 수신] 유저(%s): %s", user_id, user_text)
            request_text = backlog_request_text(user_text)
            if request_text is not None:
                handle_backlog_mention(client, say, event, request_text)
                return
            process_chat_and_reply(client, say, channel_id, thread_ts, event.get("ts"), user_text)

    # 4. /백로그 커맨드 처리 (슬래시 명령은 스레드 정보가 없어 기획서와 연결하지 않고, 내용 유사도로만 답변에 붙는다)
    @slack_app.command("/백로그")
    def handle_backlog_command(ack, command, say, client):
        ack()
        register_backlog(client, say, command.get("text", ""), command.get("user_id"), reply_ts=None,
                         reporter_name=command.get("user_name"))

    # 5. 백로그 리마인더 DM 버튼 (해결됨은 담당자만 처리 가능)
    @slack_app.action("backlog_resolve")
    def handle_backlog_resolve(ack, body, client):
        ack()
        handle_backlog_button(body, client, resolve=True)

    @slack_app.action("backlog_pending")
    def handle_backlog_pending(ack, body, client):
        ack()
        handle_backlog_button(body, client, resolve=False)

    handler = SocketModeHandler(slack_app, app_token)
    return handler

def start_slack_bot():
    h = init_slack()
    if h:
        logger.info("⚡ [Slack] Socket Mode 봇을 시작합니다.")
        h.connect()
        _reminder_stop.clear()
        threading.Thread(target=_reminder_loop, args=(slack_app.client,), daemon=True, name="backlog-reminder").start()

def stop_slack_bot():
    _reminder_stop.set()
    if handler:
        handler.close()