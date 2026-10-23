"""기획서 속성 검사 결과를 슬랙으로 보내고 버튼을 처리한다 (검사와 제안값은 property_check.py).
'DB 등록' 버튼을 누른 사람에게 DM으로 [✅ 이대로 적용] [✏️ 고쳐서 적용] [💬 대화로 고치기] [🔁 토스] [🙅 적용 안 함] 버튼이 달린 메세지를 보낸다.
토스하면 새 담당자에게 같은 메세지를 보내고, 담당자 속성을 채우는 중이었으면 그 값도 새 담당자로 바꾼다.
적용하면 노션 속성에 바로 입력하고, 기획서 이름이 바뀌었으면 그 문서를 다시 동기화한다.
그 메세지의 스레드에 쓴 말은 사서가 고칠 지시로 받아 이름/개요/상태를 고치고, 고친 값을 원래 메세지에 반영한다.

메세지는 웹훅을 받는 서버가 보내고 버튼도 같은 서버의 슬랙 앱(slack_bot.py)이 받는다. 담당자만 처리할 수 있다.
노션 사용자와 슬랙 사용자는 사서 제안과 같은 방법(이메일, 다르면 이름)으로 맞춘다 (proposal_slack.py).
노션 연동에 '콘텐츠 업데이트' 권한이 있어야 속성을 쓸 수 있다.
"""
import json
import logging
import threading
from typing import Dict, List, Optional, Tuple
import config
import indexing
import librarian_slack
import notion_parser
import notion_writer
import property_check
import proposal_slack
from property_check import NAME, SUMMARY, STATUS, OWNER, MODIFIED, ORDER, label, EMPTY, UNSUPPORTED, OPEN, APPLIED, SKIPPED

logger = logging.getLogger(__name__)

SECTION_CHARS = 2800
# 슬랙 선택 목록은 100개까지
SELECT_OPTIONS = 100
EDITABLE = (NAME, SUMMARY, STATUS)


def chat_keys(missing: List[Dict]) -> List[str]:
    """대화로 고칠 수 있는 항목: 비어 있는 이름/개요/상태 (사서가 처음에 정하지 못한 항목도 대화로 채울 수 있다)"""
    return [m["key"] for m in missing if m["reason"] == EMPTY and m["key"] in EDITABLE]


def doc_link(record: Dict) -> str:
    title = record.get("title") or "제목 없음"
    return f"<{record['url']}|{title}>" if record.get("url") else title


def proposal_lines(proposal: Dict[str, Dict]) -> str:
    return "\n".join(f"• *{label(key)}:* {proposal[key]['text']}" for key in ORDER if key in proposal)


def actions_block_id(page_id: str) -> str:
    return f"property_{page_id}"


def message_blocks(record: Dict, missing: List[Dict], requester_found: bool = True) -> List[Dict]:
    """경고, 사서가 정한 값, 버튼"""
    proposal = record["proposal"]
    empty = [m for m in missing if m["reason"] == EMPTY]
    head = [f"⚠️ {doc_link(record)} 문서에 비어 있는 속성이 있어요: *{', '.join(m['label'] for m in empty)}*"]
    if record.get("toss_note"):
        head.insert(0, record["toss_note"])
    unsupported = [m for m in missing if m["reason"] == UNSUPPORTED]
    if unsupported:
        head.append(f"(노션 DB에 '{', '.join(m['property'] for m in unsupported)}' 속성이 없거나 종류가 달라서 자동 입력은 못 해요)")
    if not requester_found:
        head.append("(받으실 분을 슬랙에서 찾지 못해 대신 보내요)")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": proposal_clip("\n".join(head))}}]

    unresolved = [m["label"] for m in empty if m["key"] not in proposal]
    if not proposal:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                       f"사서가 채울 값을 정하지 못했어요. 노션에서 직접 채워 주세요: {', '.join(unresolved)}"}})
        return blocks
    text = f"💡 *사서 제안* — 적용하면 노션에 바로 입력돼요\n{proposal_lines(proposal)}"
    if unresolved:
        text += f"\n정하지 못한 항목은 노션에서 직접 채워 주세요: {', '.join(unresolved)}"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": proposal_clip(text)}})

    buttons = [{"type": "button", "action_id": "property_apply", "style": "primary", "value": record["page_id"],
                "text": {"type": "plain_text", "text": "✅ 이대로 적용"}}]
    if any(key in proposal for key in EDITABLE):
        buttons.append({"type": "button", "action_id": "property_edit", "value": record["page_id"],
                        "text": {"type": "plain_text", "text": "✏️ 고쳐서 적용"}})
    if chat_keys(missing):
        buttons.append({"type": "button", "action_id": "property_chat", "value": record["page_id"],
                        "text": {"type": "plain_text", "text": "💬 대화로 고치기"}})
    buttons.append({"type": "button", "action_id": "property_toss", "value": record["page_id"],
                    "text": {"type": "plain_text", "text": "🔁 토스"}})
    buttons.append({"type": "button", "action_id": "property_skip", "value": record["page_id"],
                    "text": {"type": "plain_text", "text": "🙅 적용 안 함"}})
    blocks.append({"type": "actions", "block_id": actions_block_id(record["page_id"]), "elements": buttons})
    return blocks


def proposal_clip(text: str) -> str:
    return proposal_slack.clip(text, SECTION_CHARS)


def result_blocks(record: Dict, status_text: str, proposal: Optional[Dict] = None) -> List[Dict]:
    """처리한 뒤 버튼을 없애고 결과 문구와 입력된 값을 보여준다"""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ {doc_link(record)} 속성 검사"}}]
    lines = proposal_lines(proposal if proposal is not None else record.get("proposal", {}))
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": proposal_clip(lines)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": status_text}]})
    return blocks


def message_text(record: Dict) -> str:
    """묻는 메세지의 본문 (알림에 보이는 글. 내용은 블록에 있다)"""
    return f"⚠️ '{record.get('title') or '제목 없음'}' 문서에 비어 있는 속성이 있어요"


def notify(record: Dict, missing: List[Dict], requested_by: Optional[str]) -> Optional[Dict]:
    """버튼을 누른 사람에게 DM으로 보낸다. 슬랙에서 그 사람을 찾지 못하면 문서 진단 담당자에게 대신 보낸다.
    보낼 곳이 없으면 None (기록을 남기지 않아 다음 '완료' 때 다시 시도한다)"""
    client = proposal_slack.web_client()
    if client is None:
        logger.info("ℹ️ [속성 검사] 슬랙 토큰이 없어 '%s' 알림을 보내지 않습니다.", record.get("title"))
        return None
    requester = proposal_slack.slack_user_for_notion(client, requested_by) if requested_by else None
    assignee = requester or config.DIAGNOSIS_REPORT_USER_ID
    if not assignee:
        logger.warning("⚠️ [속성 검사] '%s' 알림을 받을 사람을 찾지 못했습니다.", record.get("title"))
        return None
    sent = client.chat_postMessage(channel=assignee, text=message_text(record),
                                   blocks=message_blocks(record, missing, requester_found=bool(requester)))
    logger.info("📬 [속성 검사] '%s' 비어 있는 속성 %s 을(를) %s 에게 물었습니다.", record.get("title"), record["missing"], assignee)
    return {"assignee_id": assignee, "slack_channel": sent["channel"], "slack_ts": sent["ts"], "requester_found": bool(requester)}


def close_message(record: Dict, status_text: str) -> None:
    """묻던 메세지의 버튼을 결과 문구로 바꾼다 (노션에서 직접 채운 경우 등). 실패해도 흐름에는 영향이 없다"""
    client = proposal_slack.web_client()
    if client is None or not record.get("slack_ts"):
        return
    try:
        client.chat_update(channel=record["slack_channel"], ts=record["slack_ts"], text=status_text,
                           blocks=result_blocks(record, status_text))
    except Exception as e:
        logger.warning("⚠️ [속성 검사] 메세지를 바꾸지 못했습니다: %s", e)


# --- [버튼 처리] ---
def authorize(client, channel_id: str, user_id: str, page_id: str, ts: Optional[str] = None) -> Optional[Dict]:
    """처리할 수 있는 요청이면 기록, 아니면 본인에게만 안내하고 None.
    ts는 버튼이 달린 메세지. 비어 있는 항목이 바뀌어 새로 물었다면 예전 메세지의 버튼은 받지 않는다"""
    record = property_check.get_record(page_id)
    refusal = None
    if record is None:
        refusal = "⚠️ 찾을 수 없는 요청이에요."
    elif record["status"] != OPEN:
        refusal = "이미 처리된 요청이에요."
    elif record.get("assignee_id") != user_id:
        refusal = "❌ 담당자만 처리할 수 있어요."
    elif ts and record.get("slack_ts") and ts != record["slack_ts"]:
        refusal = "이전 메세지예요. 가장 최근에 받은 메세지의 버튼을 눌러 주세요."
    if refusal:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=refusal)
        return None
    return record


def apply(record: Dict, proposal: Dict[str, Dict]) -> Tuple[List[str], bool]:
    """제안을 노션에 쓴다. 지금도 비어 있는 항목에만 쓰므로 그새 누가 채운 값은 덮어쓰지 않는다.
    (쓴 항목 이름 목록, 기획서 이름을 썼는지)"""
    token = config.NOTION_INTEGRATION_TOKEN
    page = notion_parser.fetch_page(record["page_id"], token)
    if page is None or notion_parser.is_trashed(page):
        raise RuntimeError("노션 문서를 읽지 못했어요")
    schema = property_check.database_schema(page, token)
    missing = property_check.inspect(page, schema)
    properties = property_check.notion_properties(proposal, missing, schema)
    if properties:
        notion_writer.update_page_properties(record["page_id"], properties)
    written = [m for m in missing if m["property"] in properties]
    return [m["label"] for m in written], any(m["key"] == NAME for m in written)


def apply_and_report(client, record: Dict, proposal: Dict[str, Dict], user_id: str, channel_id: str, ts: str) -> None:
    try:
        applied, renamed = apply(record, proposal)
    except Exception as e:
        logger.exception("🚨 [속성 검사] 노션 입력 실패: %s", record.get("title"))
        client.chat_postEphemeral(channel=channel_id, user=user_id,
                                  text=f"❌ 노션에 입력하지 못했어요. 잠시 후 다시 눌러 주세요. ({type(e).__name__}: {str(e)[:200]})")
        return
    property_check.close_record(record["page_id"], APPLIED, user_id)
    if applied:
        where = f"<{record['url']}|노션에서 보기>" if record.get("url") else "노션에 입력했어요"
        status_text = f"✅ 적용했어요 ({', '.join(applied)}) — {where}"
    else:
        status_text = "✅ 이미 노션에 채워져 있어서 바꾸지 않았어요."
    client.chat_update(channel=channel_id, ts=ts, text=status_text, blocks=result_blocks(record, status_text, proposal))
    if renamed:
        # 기획서 이름은 검색 색인의 문서 제목이므로 새 이름으로 다시 동기화한다 ('완료'로 보지 않으므로 검토/재질문은 일어나지 않는다)
        threading.Thread(target=indexing.sync_document_to_db, args=(record["page_id"], config.NOTION_INTEGRATION_TOKEN),
                         daemon=True, name="property-sync").start()


def pressed(body, client) -> Tuple[Optional[Dict], str, str, str]:
    """버튼 누름: (처리할 수 있으면 기록 아니면 None, 누른 사람, 채널, 버튼이 달린 메세지 ts)"""
    user_id, channel_id, ts = body["user"]["id"], body["channel"]["id"], body["message"]["ts"]
    return authorize(client, channel_id, user_id, body["actions"][0]["value"], ts), user_id, channel_id, ts


def modal_metadata(record: Dict, channel_id: str, ts: str) -> str:
    """입력창을 제출할 때 다시 authorize 하도록 버튼이 달린 메세지를 남긴다"""
    return json.dumps({"page_id": record["page_id"], "channel": channel_id, "ts": ts})


def handle_apply(body, client) -> None:
    record, user_id, channel_id, ts = pressed(body, client)
    if record:
        apply_and_report(client, record, record["proposal"], user_id, channel_id, ts)


def handle_skip(body, client) -> None:
    record, user_id, channel_id, ts = pressed(body, client)
    if not record:
        return
    property_check.close_record(record["page_id"], SKIPPED, user_id)
    status_text = "🙅 적용하지 않기로 했어요. 노션에서 직접 채우면 돼요. (같은 항목이 비어 있는 동안은 다시 묻지 않아요)"
    client.chat_update(channel=channel_id, ts=ts, text=status_text, blocks=result_blocks(record, status_text))


def edit_view(record: Dict, metadata: str) -> Dict:
    """고쳐서 적용: 이름/개요/상태를 고칠 수 있는 입력창. 담당자와 마지막 수정일은 정한 대로 입력된다"""
    proposal = record["proposal"]
    blocks = []
    fixed = proposal_lines({key: proposal[key] for key in (OWNER, MODIFIED) if key in proposal})
    if fixed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"이 항목은 그대로 입력돼요\n{fixed}"}})
    if NAME in proposal:
        blocks.append({"type": "input", "block_id": NAME, "label": {"type": "plain_text", "text": label(NAME)},
                       "element": {"type": "plain_text_input", "action_id": "value", "initial_value": proposal[NAME]["text"]}})
    if SUMMARY in proposal:
        blocks.append({"type": "input", "block_id": SUMMARY, "label": {"type": "plain_text", "text": label(SUMMARY)},
                       "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                                   "max_length": property_check.SUMMARY_CHARS, "initial_value": proposal[SUMMARY]["text"]}})
    if STATUS in proposal:
        options = [{"text": {"type": "plain_text", "text": name[:75]}, "value": name} for name in record.get("status_options", [])[:SELECT_OPTIONS]]
        chosen = next((o for o in options if o["value"] == proposal[STATUS]["value"]), None)
        element = {"type": "static_select", "action_id": "value", "options": options}
        if chosen:
            element["initial_option"] = chosen
        blocks.append({"type": "input", "block_id": STATUS, "label": {"type": "plain_text", "text": label(STATUS)}, "element": element})
    return {"type": "modal", "callback_id": "property_edit_submit", "private_metadata": metadata,
            "title": {"type": "plain_text", "text": "속성 고쳐서 적용"},
            "submit": {"type": "plain_text", "text": "적용"}, "close": {"type": "plain_text", "text": "취소"}, "blocks": blocks}


def handle_edit(body, client) -> None:
    record, _, channel_id, ts = pressed(body, client)
    if record:
        client.views_open(trigger_id=body["trigger_id"], view=edit_view(record, modal_metadata(record, channel_id, ts)))


def edited_proposal(record: Dict, values: Dict) -> Dict[str, Dict]:
    """입력창에서 고친 값을 반영한 제안. 비워 둔 항목은 입력하지 않는다"""
    proposal = {key: item for key, item in record["proposal"].items() if key not in EDITABLE}
    for key in (NAME, SUMMARY):
        if key in record["proposal"]:
            text = ((values.get(key, {}).get("value") or {}).get("value") or "").strip()
            if text:
                proposal[key] = {"text": text, "value": text}
    if STATUS in record["proposal"]:
        chosen = ((values.get(STATUS, {}).get("value") or {}).get("selected_option") or {}).get("value")
        if chosen:
            proposal[STATUS] = {"text": chosen, "value": chosen}
    return proposal


def handle_edit_submit(body, client) -> None:
    view = body["view"]
    meta = json.loads(view["private_metadata"])
    user_id = body["user"]["id"]
    record = authorize(client, meta["channel"], user_id, meta["page_id"], meta["ts"])
    if record:
        apply_and_report(client, record, edited_proposal(record, view["state"]["values"]), user_id, meta["channel"], meta["ts"])


# --- [대화로 고치기] ---
# 묻는 메세지는 문서마다 하나라서 그 메세지의 스레드를 그대로 대화 스레드로 쓴다 (사서 제안처럼 새 스레드를 열지 않는다).
# 고친 값은 원래 메세지에 반영하므로 버튼은 늘 원래 메세지의 것 하나뿐이다.
REVISE_PROMPT = (
    "너는 게임 기획팀의 기획서 정리 담당 '사서'다. 노션 기획서의 비어 있는 속성 값을 제안했고, 담당 기획자가 대화로 함께 고치고 있다. "
    "[기획자의 말]에 따라 [고칠 수 있는 항목]의 값을 고쳐라. 문서 내용을 확인해야 하면 도구로 [문서]를 찾아 읽어라.\n"
    "- name: 기획서 이름. 문서가 무엇을 정의하는지 보이는 짧은 명사구 (30자 이내).\n"
    "- summary: 개요. 문서에 적힌 사실만 쓴다. 기획자가 정한 내용은 그대로 따른다.\n"
    "- status: 상태. [상태 후보] 중 하나만 쓴다. 후보에 없는 값을 요청받으면 바꾸지 말고 후보를 알려 준다.\n"
    "- 기획자의 말이 질문이면 답만 하고 값은 바꾸지 않는다.\n"
    "- 담당자와 마지막 수정일은 대화로 고칠 수 없다. 요청받으면 노션에서 직접 바꾸면 된다고 알린다.\n"
    "JSON만 출력한다: {\"reply\": \"기획자에게 할 말 (무엇을 바꿨는지 또는 질문에 대한 답) 2문장 이내\", "
    "\"values\": {\"바꾼 항목 (name/summary/status)\": \"새 값\"}} (바꾸지 않았으면 values는 빈 객체)"
)


def handle_chat(body, client) -> None:
    """대화로 고치기: 묻는 메세지의 스레드에 고칠 점을 말해 달라고 안내한다"""
    record, user_id, channel_id, _ = pressed(body, client)
    if not record:
        return
    if "chat_history" in record:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text="이미 이 메세지의 스레드에서 대화하고 있어요. 그 스레드에서 이어서 말해 주세요.")
        return
    client.chat_postMessage(channel=channel_id, thread_ts=record["slack_ts"], text=(
        "💬 이 스레드에 고칠 점을 말해 주세요. (예: '개요를 한 문장으로 줄여 줘', '상태는 검토 중으로 해 줘')\n"
        "사서가 고친 값은 위 메세지에 바로 반영돼요. 마음에 들면 위 메세지의 [✅ 이대로 적용]을 눌러 주세요."))
    property_check.update_record(record["page_id"], chat_history=[])


def checked_values(values: Dict, keys: List[str], options: List[str]) -> Dict[str, str]:
    """사서가 고친 값 중 쓸 수 있는 것만: 고칠 수 있는 항목이고, 상태는 선택지에 있는 값"""
    checked = {}
    for key in keys:
        value = str((values or {}).get(key) or "").strip()
        if not value or (key == STATUS and value not in options):
            continue
        checked[key] = value[:property_check.SUMMARY_CHARS] if key == SUMMARY else value
    return checked


def revise(record: Dict, message: str) -> Dict:
    """사서가 기획자의 말에 따라 값을 고친다. {"reply", "values": {항목: 새 값}}"""
    import librarian_report
    import review
    keys = chat_keys(property_check.missing_items(record))
    proposal = record["proposal"]
    current = "\n".join(f"- {key} ({label(key)}): {proposal[key]['text'] if key in proposal else '(정하지 못함)'}" for key in keys)
    request = (f"[문서] {record.get('title') or '제목 없음'}\n[고칠 수 있는 항목과 지금 값]\n{current}\n"
               f"[상태 후보] {', '.join(record.get('status_options', [])) or '(없음)'}\n\n"
               f"[이 스레드의 이전 대화]\n{librarian_slack.history_text(record.get('chat_history', []))}\n\n[기획자의 말]\n{message}")
    data = review.parse_json(review.run_with_tools(REVISE_PROMPT, request, f"'{record.get('title')}' 속성 대화로 고치기",
                                                   librarian_report.REVISE))
    return {"reply": data.get("reply") or "고쳐 봤어요.",
            "values": checked_values(data.get("values"), keys, record.get("status_options", []))}


def handle_chat_message(client, event) -> bool:
    """DM 스레드 메세지가 속성 검사 메세지의 스레드에 쓴 것이면 고칠 지시로 처리하고 True. 아니면 False (일반 질문으로 처리)"""
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return False
    record = property_check.record_by_message(thread_ts)
    if record is None:
        return False
    channel_id, user_id, text = event["channel"], event.get("user"), event.get("text", "")
    if record["status"] != OPEN or record.get("assignee_id") != user_id:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                text="이미 처리된 요청이에요." if record["status"] != OPEN else "담당자만 고칠 수 있어요.")
        return True
    turn = librarian_slack.chat_turn(client, channel_id, thread_ts, record.get("title") or "제목 없음", record.get("chat_history", []), text,
                                     lambda: revise(record, text))
    if turn is None:
        return True
    result, history, placeholder_ts = turn
    proposal = {**record["proposal"], **{key: {"text": value, "value": value} for key, value in result["values"].items()}}
    property_check.update_record(record["page_id"], proposal=proposal, chat_history=history)
    record.update(proposal=proposal, chat_history=history)
    if not result["values"]:
        client.chat_update(channel=channel_id, ts=placeholder_ts, text=f"💬 {result['reply']}")
        return True
    client.chat_update(channel=record["slack_channel"], ts=record["slack_ts"], text=message_text(record),
                       blocks=message_blocks(record, property_check.missing_items(record), record.get("requester_found", True)))
    changed = proposal_lines({key: proposal[key] for key in result["values"]})
    client.chat_update(channel=channel_id, ts=placeholder_ts, text=proposal_clip(
        f"✏️ {result['reply']}\n{changed}\n위 메세지에 반영했어요. 마음에 들면 [✅ 이대로 적용]을 눌러 주세요."))
    return True


# --- [토스: 다른 담당자에게 넘기기] ---
def handle_toss(body, client) -> None:
    """토스: 넘길 사람과 한마디를 묻는다"""
    record, _, channel_id, ts = pressed(body, client)
    if record:
        client.views_open(trigger_id=body["trigger_id"], view=librarian_slack.toss_view(
            "property_toss_submit", proposal_clip(f"*{record.get('title') or '제목 없음'}* 속성 검사"),
            modal_metadata(record, channel_id, ts)))


def tossed_proposal(client, record: Dict, to_user: str) -> Dict[str, Dict]:
    """담당자를 채우는 중이었으면 담당자 값을 새 담당자로 바꾼 제안. 노션에서 그 사람을 찾지 못하면 담당자는 노션에서 직접 채우게 뺀다"""
    proposal = dict(record["proposal"])
    if not any(m["key"] == OWNER and m["reason"] == EMPTY for m in property_check.missing_items(record)):
        return proposal
    proposal.pop(OWNER, None)
    notion_user = proposal_slack.notion_user_for_slack(client, to_user)
    if notion_user:
        proposal[OWNER] = {"text": proposal_slack.user_name(client, to_user) or "(노션 사용자)", "value": notion_user}
    return proposal


def handle_toss_submit(body, client) -> None:
    """담당자를 바꾸고, 새 담당자에게 같은 메세지를 보내고, 원래 메세지에 넘겼다고 남긴다.
    대화로 고치던 중이었으면 고친 값은 넘어가고, 새 담당자는 새 메세지의 스레드에서 이어서 고친다."""
    meta, to_user, memo = librarian_slack.toss_input(body["view"])
    user_id = body["user"]["id"]
    if not to_user or to_user == user_id:
        return
    record = authorize(client, meta["channel"], user_id, meta["page_id"], meta["ts"])
    if not record:
        return
    if "chat_history" in record:
        client.chat_postMessage(channel=meta["channel"], thread_ts=record["slack_ts"],
                                text=f"🔁 <@{to_user}> 님에게 넘겼어요. 이 스레드는 더 이상 쓰지 않아요.")
        del record["chat_history"]
    record.update(proposal=tossed_proposal(client, record, to_user), assignee_id=to_user, requester_found=True,
                  toss_history=librarian_slack.toss_history(record, user_id, to_user, memo),
                  toss_note=librarian_slack.toss_notice(user_id, "기획서 속성 검사를", memo) + "\n담당이 아니면 다시 [🔁 토스]로 넘길 수 있어요.")
    sent = client.chat_postMessage(channel=to_user, text=message_text(record), blocks=message_blocks(record, property_check.missing_items(record)))
    record.update(slack_channel=sent["channel"], slack_ts=sent["ts"])
    property_check.save_record(record)
    status_text = f"🔁 <@{to_user}> 님에게 넘겼어요."
    client.chat_update(channel=meta["channel"], ts=meta["ts"], text=status_text, blocks=result_blocks(record, status_text))
    logger.info("🔁 [속성 검사] %s → %s: %s", user_id, to_user, record.get("title"))
