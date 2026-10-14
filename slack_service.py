"""슬랙 질문 → 기획서 답변 흐름: 스레드 대화 기록 수집, '분석 중' 메세지를 답변/도판으로 교체"""
import logging
import re
import requests
import ai_engine
import backlog
import config
import notion_parser

logger = logging.getLogger(__name__)

def format_markdown_for_slack(text: str) -> str:
    if not text: return ""
    # 이미지는 링크 변환보다 먼저 제거해야 `![..](..)`가 링크로 바뀌지 않음
    text = re.sub(r'!\[[^\]]*\]\(https?://[^\s)]+\)', '', text)
    text = re.sub(r'\[([^\]]*)\]\((https?://[^\s)]+)\)', r'<\2|🔗 \1>', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)
    text = text.replace("- 참고 문서 :", "• *참고 문서:*")
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
    """어떤 단계에서 실패하더라도 사용자가 '분석 중' 메세지만 보고 기다리지 않도록 실패 원인을 스레드에 안내한다.
    '분석 중' 메세지를 이미 보냈다면 그 메세지를 실패 안내로 바꾼다."""
    sent = {}
    try:
        _process_chat_and_reply(client, say, channel_id, thread_ts, message_ts, raw_text, sent)
    except Exception as e:
        logger.exception("🚨 [Slack] 메세지 처리 실패")
        failure = ai_engine.describe_failure(e)
        text = failure_message("메세지 처리", failure["reason"], failure["hint"])
        if sent.get("placeholder_ts"):
            try:
                client.chat_update(channel=channel_id, ts=sent["placeholder_ts"], text=text)
                return
            except Exception:
                logger.exception("🚨 [Slack] '분석 중' 메세지를 실패 안내로 바꾸지 못해 새로 보냅니다")
        try:
            say(text=text, thread_ts=thread_ts)
        except Exception:
            logger.exception("🚨 [Slack] 실패 안내 메세지 전송도 실패")

def _process_chat_and_reply(client, say, channel_id, thread_ts, message_ts, raw_text, sent):
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
            if msg_text.startswith(backlog.BACKLOG_KEYWORD) or msg_text.startswith(backlog.BACKLOG_CONFIRM_PREFIX):
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
    placeholder = say(text="🤔 기획서를 읽고 분석 중입니다...", thread_ts=thread_ts)
    placeholder_ts = sent["placeholder_ts"] = placeholder["ts"]

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
