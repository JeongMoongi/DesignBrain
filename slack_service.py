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

BLOCK_ID_PATTERN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

def image_filename(img) -> str:
    """업로드 파일명에 노션 이미지 block_id를 넣어 이후 스레드에서 중복 첨부 여부를 판별"""
    return f"{img.get('caption', '기획 도판')}_{img.get('block_id')}.jpg"

def collect_page_images(page_ids, page_images, shared_block_ids, limit):
    """유사도 순으로 페이지를 돌며 스레드에 아직 공유되지 않은 이미지를 최대 limit장 수집.
    page_images에 캐싱된 block_id가 있으면 그 블록만 조회하고, 없으면 페이지 전체를 탐색한다."""
    token = config.NOTION_INTEGRATION_TOKEN
    if not token:
        return []

    collected = []
    seen_block_ids = set()
    for page_id in page_ids:
        cached = page_images.get(page_id)
        try:
            candidates = cached if cached is not None else notion_parser.fetch_page_images(page_id, token)
        except Exception as e:
            logger.warning("⚠️ [Notion] 이미지 조회 실패 (page_id: %s): %s", page_id, e)
            continue

        for img in candidates:
            block_id = img.get("block_id")
            if not block_id or block_id in seen_block_ids or block_id in shared_block_ids:
                continue
            seen_block_ids.add(block_id)
            if cached is not None:
                # 캐시에는 만료되는 URL이 없으므로 블록을 조회해 새 URL을 받는다
                try:
                    img = notion_parser.fetch_image_by_block_id(block_id, token)
                except Exception as e:
                    logger.warning("⚠️ [Notion] 이미지 블록 조회 실패 (block_id: %s): %s", block_id, e)
                    img = None
                if not img:
                    continue
            collected.append(img)
            if len(collected) >= limit:
                return collected
    return collected

def failure_message(stage, reason, hint):
    return f"❌ 답변을 만들지 못했어요.\n• *실패 단계:* {stage}\n• *원인:* {reason}\n{hint}"

def process_chat_and_reply(client, say, channel_id, thread_ts, raw_text):
    """어떤 단계에서 실패하더라도 사용자가 '분석 중' 메세지만 보고 기다리지 않도록 실패 원인을 스레드에 안내한다"""
    try:
        _process_chat_and_reply(client, say, channel_id, thread_ts, raw_text)
    except Exception as e:
        logger.exception("🚨 [Slack] 메세지 처리 실패")
        failure = ai_engine.describe_failure(e)
        try:
            say(text=failure_message("메세지 처리", failure["reason"], failure["hint"]), thread_ts=thread_ts)
        except Exception:
            logger.exception("🚨 [Slack] 실패 안내 메세지 전송도 실패")

def _process_chat_and_reply(client, say, channel_id, thread_ts, raw_text):
    cleaned_query = re.sub(r'<@.*?>', '', raw_text).strip()

    chat_history = []
    full_thread_context = []
    shared_block_ids = set()

    try:
        replies = client.conversations_replies(channel=channel_id, ts=thread_ts)
        messages = replies.get("messages", [])

        for msg in messages:
            # 스레드에 이미 첨부된 노션 이미지 추적 (파일 업로드 메세지는 본문이 비어 있으므로 본문 검사보다 먼저 수행)
            for f in msg.get("files", []):
                shared_block_ids.update(BLOCK_ID_PATTERN.findall(f.get("name", "")))

            msg_text = re.sub(r'<@.*?>', '', msg.get("text", "")).strip()
            if not msg_text or "기획서를 읽고 분석 중입니다" in msg_text:
                continue
            # 백로그 명령/등록 안내는 질문 맥락이 아니므로 대화 기록에서 뺀다
            if msg_text.startswith(BACKLOG_KEYWORD) or msg_text.startswith(BACKLOG_CONFIRM_PREFIX):
                continue

            role = "user" if "bot_id" not in msg else "assistant"
            chat_history.append({"role": role, "content": msg_text})
            if role == "user":
                full_thread_context.append(msg_text)
    except Exception as e:
        logger.info("스레드 히스토리 수집 생략 (DM 또는 단발 메세지): %s", e)

    final_query = cleaned_query
    if not final_query and full_thread_context:
        final_query = full_thread_context[-1]

    if not final_query:
        say(text="네, 기획자님! 무엇이 궁금하신가요?", thread_ts=thread_ts)
        return

    say(text=f"🤔 기획서를 읽고 분석 중입니다...", thread_ts=thread_ts)

    res = ai_engine.ask_ai_with_history(final_query, chat_history[:-1])

    if res.get("status") == "error":
        say(text=failure_message(res["stage"], res["reason"], res["hint"]), thread_ts=thread_ts)
        return

    answer = res.get("AI_답변", "")

    formatted_answer = format_markdown_for_slack(answer)
    say(text=formatted_answer, thread_ts=thread_ts)

    # 답변 전송 후, 검색된 문서의 이미지를 노션에서 직접 가져온다 (노션 파일 URL은 만료되므로 DB에 저장하지 않음)
    image_objs = collect_page_images(res.get("page_ids", []), res.get("page_images", {}), shared_block_ids, limit=2)

    # 미공유된 신규 이미지 파일만 최대 2장 슬랙 전송
    if image_objs:
        logger.info("📤 [Slack] 신규 연관 기획 도판 %d장 전송", len(image_objs))

        for img in image_objs:
            img_url = img.get("url")
            caption = img.get("caption", "기획 도판")

            try:
                img_res = requests.get(img_url, timeout=15)
                if img_res.status_code == 200:
                    client.files_upload_v2(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        file=img_res.content,
                        filename=image_filename(img),
                        title=f"📸 {caption}"
                    )
            except Exception as img_err:
                logger.error("🚨 [Slack] 이미지 파일 업로드 실패: %s", img_err)


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
    """스레드에서 이 명령 직전의 봇 답변이 참고한 기획서 (답변 끝의 노션 링크로 판별)"""
    messages = client.conversations_replies(channel=channel_id, ts=thread_ts).get("messages", [])
    for msg in reversed(messages):
        if "bot_id" in msg and float(msg.get("ts", 0)) < float(before_ts):
            page_ids = backlog.page_ids_in_text(msg.get("text", ""))
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
    def handle_app_mention_events(body, client, say):
        event = body.get("event", {})
        channel_id = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        text = event.get("text", "")

        logger.info("📢 [채널 멘션 수신] %s", text)
        request_text = backlog_request_text(text)
        if request_text is not None:
            handle_backlog_mention(client, say, event, request_text)
            return
        process_chat_and_reply(client, say, channel_id, thread_ts, text)

    # 3. 1:1 DM 메세지 처리
    @slack_app.event("message")
    def handle_message_events(body, client, say):
        event = body.get("event", {})
        channel_type = event.get("channel_type")
        
        # 봇 자신의 메세지가 아니고, 순수 DM(im)인 경우만 응답
        if channel_type == "im" and not event.get("bot_id") and not event.get("subtype"):
            user_text = event.get("text", "")
            user_id = event.get("user")
            channel_id = event.get("channel")
            thread_ts = event.get("ts")
            
            logger.info("💬 [DM 수신] 유저(%s): %s", user_id, user_text)
            request_text = backlog_request_text(user_text)
            if request_text is not None:
                handle_backlog_mention(client, say, event, request_text)
                return
            process_chat_and_reply(client, say, channel_id, thread_ts, user_text)

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