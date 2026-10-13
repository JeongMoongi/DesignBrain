import re
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import ai_engine
import config
import notion_parser

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
            print(f"⚠️ [Notion] 이미지 조회 실패 (page_id: {page_id}): {e}")
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
                    print(f"⚠️ [Notion] 이미지 블록 조회 실패 (block_id: {block_id}): {e}")
                    img = None
                if not img:
                    continue
            collected.append(img)
            if len(collected) >= limit:
                return collected
    return collected

def process_chat_and_reply(client, say, channel_id, thread_ts, raw_text):
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

            role = "user" if "bot_id" not in msg else "assistant"
            chat_history.append({"role": role, "content": msg_text})
            if role == "user":
                full_thread_context.append(msg_text)
    except Exception as e:
        print(f"⚠️ 히스토리 수집 (DM 또는 단발 메세지): {e}")

    final_query = cleaned_query
    if not final_query and full_thread_context:
        final_query = full_thread_context[-1]

    if not final_query:
        say(text="네, 기획자님! 무엇이 궁금하신가요?", thread_ts=thread_ts)
        return

    say(text=f"🤔 기획서를 읽고 분석 중입니다...", thread_ts=thread_ts)

    res = ai_engine.ask_ai_with_history(final_query, chat_history[:-1])

    if res.get("status") == "error":
        say(text=f"❌ 에러 발생: {res.get('message')}", thread_ts=thread_ts)
        return

    answer = res.get("AI_답변", "")

    formatted_answer = format_markdown_for_slack(answer)
    say(text=formatted_answer, thread_ts=thread_ts)

    # 답변 전송 후, 검색된 문서의 이미지를 노션에서 직접 가져온다 (노션 파일 URL은 만료되므로 DB에 저장하지 않음)
    image_objs = collect_page_images(res.get("page_ids", []), res.get("page_images", {}), shared_block_ids, limit=2)

    # 미공유된 신규 이미지 파일만 최대 2장 슬랙 전송
    if image_objs:
        print(f"📤 [Slack] 신규 연관 기획 도판 {len(image_objs)}장 전송 시작...")

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
                print(f"🚨 [Slack] 이미지 파일 업로드 실패: {img_err}")


def init_slack():
    global slack_app, handler
    
    bot_token = config.SLACK_BOT_TOKEN
    app_token = config.SLACK_APP_TOKEN
    
    if not bot_token or not app_token:
        print("⚠️ [Slack] 토큰 미설정")
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
        
        print(f"📢 [채널 멘션 수신] {text}")
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
            
            print(f"💬 [DM 수신] 유저({user_id}): {user_text}")
            process_chat_and_reply(client, say, channel_id, thread_ts, user_text)

    # 4. /백로그 커맨드 처리
    @slack_app.command("/백로그")
    def handle_backlog_command(ack, command, say):
        ack()
        user_id = command.get("user_id")
        text = command.get("text", "").strip()
        parts = text.split(" ", 1)
        if len(parts) < 2:
            say(text=f"<@{user_id}> ❌ 형식 오류! `/백로그 [담당자] [내용]` 형태로 입력해주세요.")
            return

        assignee, content = parts[0], parts[1]
        try:
            target_title = ai_engine.add_backlog(assignee, content)
            msg = f"💡 *[기획 반창고(백로그) 등록 완료]*\n• *대상 문서:* `{target_title}`\n• *담당자:* {assignee}\n• *내용:* {content}"
            say(text=msg)
        except Exception as e:
            say(text=f"❌ 백로그 등록 실패: {e}")

    handler = SocketModeHandler(slack_app, app_token)
    return handler

def start_slack_bot():
    h = init_slack()
    if h:
        print("⚡ [Slack] Socket Mode 봇이 서브 스레드에서 시작되었습니다!")
        h.connect()