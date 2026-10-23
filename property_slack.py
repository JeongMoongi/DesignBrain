"""기획서 속성 검사 결과를 슬랙으로 보내고 버튼을 처리한다 (검사와 제안값은 property_check.py).
'DB 등록' 버튼을 누른 사람에게 DM으로 [✅ 이대로 적용] [✏️ 고쳐서 적용] [🙅 적용 안 함] 버튼이 달린 메세지를 보낸다.
적용하면 노션 속성에 바로 입력하고, 기획서 이름이 바뀌었으면 그 문서를 다시 동기화한다.

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


def doc_link(record: Dict) -> str:
    title = record.get("title") or "제목 없음"
    return f"<{record['url']}|{title}>" if record.get("url") else title


def proposal_lines(proposal: Dict[str, Dict]) -> str:
    return "\n".join(f"• *{label(key)}:* {proposal[key]['text']}" for key in ORDER if key in proposal)


def actions_block_id(page_id: str) -> str:
    return f"property_{page_id}"


def message_blocks(record: Dict, missing: List[Dict], requester_found: bool = True) -> List[Dict]:
    """경고, AI가 정한 값, 버튼"""
    proposal = record["proposal"]
    empty = [m for m in missing if m["reason"] == EMPTY]
    head = [f"⚠️ {doc_link(record)} 문서에 비어 있는 속성이 있어요: *{', '.join(m['label'] for m in empty)}*"]
    unsupported = [m for m in missing if m["reason"] == UNSUPPORTED]
    if unsupported:
        head.append(f"(노션 DB에 '{', '.join(m['property'] for m in unsupported)}' 속성이 없거나 종류가 달라서 자동 입력은 못 해요)")
    if not requester_found:
        head.append("(받으실 분을 슬랙에서 찾지 못해 대신 보내요)")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": proposal_clip("\n".join(head))}}]

    unresolved = [m["label"] for m in empty if m["key"] not in proposal]
    if not proposal:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                       f"AI가 채울 값을 정하지 못했어요. 노션에서 직접 채워 주세요: {', '.join(unresolved)}"}})
        return blocks
    text = f"💡 *AI 제안* — 적용하면 노션에 바로 입력돼요\n{proposal_lines(proposal)}"
    if unresolved:
        text += f"\n정하지 못한 항목은 노션에서 직접 채워 주세요: {', '.join(unresolved)}"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": proposal_clip(text)}})

    buttons = [{"type": "button", "action_id": "property_apply", "style": "primary", "value": record["page_id"],
                "text": {"type": "plain_text", "text": "✅ 이대로 적용"}}]
    if any(key in proposal for key in EDITABLE):
        buttons.append({"type": "button", "action_id": "property_edit", "value": record["page_id"],
                        "text": {"type": "plain_text", "text": "✏️ 고쳐서 적용"}})
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
    sent = client.chat_postMessage(channel=assignee, text=f"⚠️ '{record.get('title') or '제목 없음'}' 문서에 비어 있는 속성이 있어요",
                                   blocks=message_blocks(record, missing, requester_found=bool(requester)))
    logger.info("📬 [속성 검사] '%s' 비어 있는 속성 %s 을(를) %s 에게 물었습니다.", record.get("title"), record["missing"], assignee)
    return {"assignee_id": assignee, "slack_channel": sent["channel"], "slack_ts": sent["ts"]}


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
def authorize(client, channel_id: str, user_id: str, page_id: str) -> Optional[Dict]:
    """처리할 수 있는 요청이면 기록, 아니면 본인에게만 안내하고 None"""
    record = property_check.get_record(page_id)
    refusal = None
    if record is None:
        refusal = "⚠️ 찾을 수 없는 요청이에요."
    elif record["status"] != OPEN:
        refusal = "이미 처리된 요청이에요."
    elif record.get("assignee_id") != user_id:
        refusal = "❌ 담당자만 처리할 수 있어요."
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


def handle_apply(body, client) -> None:
    user_id, channel_id, page_id = body["user"]["id"], body["channel"]["id"], body["actions"][0]["value"]
    record = authorize(client, channel_id, user_id, page_id)
    if record:
        apply_and_report(client, record, record["proposal"], user_id, channel_id, body["message"]["ts"])


def handle_skip(body, client) -> None:
    user_id, channel_id, page_id = body["user"]["id"], body["channel"]["id"], body["actions"][0]["value"]
    record = authorize(client, channel_id, user_id, page_id)
    if not record:
        return
    property_check.close_record(page_id, SKIPPED, user_id)
    status_text = "🙅 적용하지 않기로 했어요. 노션에서 직접 채우면 돼요. (같은 항목이 비어 있는 동안은 다시 묻지 않아요)"
    client.chat_update(channel=channel_id, ts=body["message"]["ts"], text=status_text, blocks=result_blocks(record, status_text))


def edit_view(record: Dict, metadata: Dict) -> Dict:
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
    return {"type": "modal", "callback_id": "property_edit_submit", "private_metadata": json.dumps(metadata),
            "title": {"type": "plain_text", "text": "속성 고쳐서 적용"},
            "submit": {"type": "plain_text", "text": "적용"}, "close": {"type": "plain_text", "text": "취소"}, "blocks": blocks}


def handle_edit(body, client) -> None:
    user_id, channel_id, page_id = body["user"]["id"], body["channel"]["id"], body["actions"][0]["value"]
    record = authorize(client, channel_id, user_id, page_id)
    if record:
        client.views_open(trigger_id=body["trigger_id"], view=edit_view(
            record, {"page_id": page_id, "channel": channel_id, "ts": body["message"]["ts"]}))


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
    record = authorize(client, meta["channel"], user_id, meta["page_id"])
    if record:
        apply_and_report(client, record, edited_proposal(record, view["state"]["values"]), user_id, meta["channel"], meta["ts"])
