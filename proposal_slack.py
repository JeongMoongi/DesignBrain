"""사서 제안을 슬랙으로 보내고 처리한다.
'완료'한 사람에게 DM 한 스레드로 묶어 보내고, 항목마다 [이대로 통과] [고쳐서 통과] [보류] [반려] 버튼을 단다.
통과하면 노션에 바로 쓰고(notion_writer) 그 문서를 다시 동기화해 정리·검토가 이어지게 한다.

사서 프로세스(librarian.py)는 슬랙 앱 없이 web_client()로 메세지만 보내고, 버튼은 슬랙 앱(slack_bot.py)이 받는다.
노션 사용자와 슬랙 사용자는 이메일로 맞춘다 (슬랙 앱에 users:read.email 권한 필요)."""
import json
import logging
import threading
from typing import Dict, List, Optional
import backlog
import config
import indexing
import librarian_report
import librarian_slack
import notion_writer
import slack_service

logger = logging.getLogger(__name__)

# 슬랙 섹션 글자 수 제한(3000)과 모달 입력칸 제한(3000) 안쪽으로 자른다
SECTION_CHARS = 2800
INPUT_CHARS = 3000
TYPE_LABELS = {backlog.DECISION: "결정 기록에 추가", backlog.WRITING: "새 문서로 작성"}
CATEGORY_ICONS = {backlog.CONFLICT: "🔀", backlog.MISSING: "🕳️", backlog.TOPIC: "📚"}


def web_client():
    """사서 프로세스에서 쓰는 슬랙 클라이언트 (토큰이 없으면 None)"""
    if not config.SLACK_BOT_TOKEN:
        return None
    from slack_sdk import WebClient
    return WebClient(token=config.SLACK_BOT_TOKEN)


def user_name(client, user_id):
    return slack_service.user_name(client, user_id)


def slack_names(member: Dict) -> List[str]:
    profile = member.get("profile", {})
    return [member.get("real_name"), profile.get("real_name"), profile.get("display_name")]


def slack_user_for_notion(client, notion_user_id: Optional[str]) -> Optional[str]:
    """노션 사용자와 이메일이 같은 슬랙 사용자, 없으면 이름이 같은 사람이 한 명뿐일 때 그 사람"""
    info = notion_writer.user_info(notion_user_id) if notion_user_id else {}
    if not info:
        return None
    if info.get("email"):
        try:
            return client.users_lookupByEmail(email=info["email"])["user"]["id"]
        except Exception as e:
            logger.info("ℹ️ [Slack] 이메일로 찾지 못해 이름으로 찾습니다 (%s): %s", info["name"], e)
    try:
        members = [m for page in client.users_list(limit=200) for m in page["members"]
                   if not m.get("is_bot") and not m.get("deleted")]
    except Exception as e:
        logger.warning("⚠️ [Slack] 사용자 목록 조회 실패: %s", e)
        return None
    wanted = notion_writer.normalize_person(info.get("name"))
    matched = [m["id"] for m in members if wanted and wanted in map(notion_writer.normalize_person, slack_names(m))]
    return matched[0] if len(matched) == 1 else None


def notion_user_for_slack(client, slack_user_id: str) -> Optional[str]:
    try:
        member = client.users_info(user=slack_user_id)["user"]
    except Exception as e:
        logger.warning("⚠️ [Slack] 사용자 정보 조회 실패 (%s): %s", slack_user_id, e)
        return None
    return notion_writer.user_id_by(member.get("profile", {}).get("email"), slack_names(member))


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def doc_links(docs: List[Dict]) -> str:
    return ", ".join(f"<{d['url']}|{d['title']}>" if d.get("url") else d["title"] for d in docs)


def headline(record: Dict) -> str:
    """제안을 한눈에 알 수 있는 한 줄: 아이콘 + 사서의 한 줄 요약. 요약이 없는 예전 제안은 제목"""
    if not record.get("summary"):
        return record["content"]
    return f"{CATEGORY_ICONS.get(record.get('category'), '📝')} {record['summary']}"


def item_blocks(record: Dict) -> List[Dict]:
    """제안 하나: 한 줄 요약·왜 문제인지·참고 문서, 사서 제안, 버튼"""
    what = TYPE_LABELS[record["proposal_type"]]
    if record["proposal_type"] == backlog.WRITING:
        what += f" ('{record['new_doc_title']}')"
    head = [f"*{headline(record)}*", record.get("problem", "")]
    if record.get("docs"):
        head.append(f"참고 문서: {doc_links(record['docs'])}")
    proposal = f"💡 *사서 제안* — 통과하면 {what}\n```{record['proposal']}```"
    if record.get("assumptions"):
        proposal = f"⚠️ *근거 없이 정한 부분:* {record['assumptions']}\n" + proposal
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": clip("\n".join(filter(None, head)), SECTION_CHARS)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": clip(proposal, SECTION_CHARS)}},
        {"type": "actions", "block_id": f"backlog_{record['backlog_id']}", "elements": [
            {"type": "button", "action_id": "proposal_approve", "style": "primary", "value": record["backlog_id"],
             "text": {"type": "plain_text", "text": "✅ 이대로 통과"}},
            {"type": "button", "action_id": "proposal_edit", "value": record["backlog_id"],
             "text": {"type": "plain_text", "text": "✏️ 고쳐서 통과"}},
            {"type": "button", "action_id": "proposal_chat", "value": record["backlog_id"],
             "text": {"type": "plain_text", "text": "💬 대화로 고치기"}},
            {"type": "button", "action_id": "proposal_toss", "value": record["backlog_id"],
             "text": {"type": "plain_text", "text": "🔁 토스"}},
            {"type": "button", "action_id": "proposal_hold", "value": record["backlog_id"],
             "text": {"type": "plain_text", "text": "⏸️ 보류"}},
            {"type": "button", "action_id": "proposal_reject", "style": "danger", "value": record["backlog_id"],
             "text": {"type": "plain_text", "text": "❌ 반려"}},
        ]},
    ]


def send_review(client, assignee_id: Optional[str], target: Dict, records: List[Dict], requester_found: bool = True,
                unreadable: str = "", intro: str = "") -> None:
    """제안을 담당자에게 한 스레드로 보낸다. requester_found가 아니면 받을 사람 대신 받은 것이라고 알린다.
    unreadable(AI가 읽지 못한 부분 안내)은 첫 메세지에 붙인다. 제안이 없어도 이 안내만 보낸다.
    intro를 주면 첫 메세지 문구를 바꾼다 (기본: '완료'한 문서를 정리하며 찾은 것)."""
    channel = assignee_id
    if not channel:
        logger.warning("⚠️ [사서] 제안을 보낼 사람이 없습니다 ('%s' %d건)", target["title"], len(records))
        return
    link = f"<{target['url']}|{target['title']}>"
    lines = [intro or (f"🧹 {link} 을(를) 정리하면서 확인이 필요한 것 *{len(records)}건*을 찾았어요.\n"
                       + "".join(f"{no}. {headline(r)}\n" for no, r in enumerate(records, 1))
                       + "통과하면 노션에 바로 반영돼요. 스레드에서 하나씩 처리해 주세요." if records else f"🧹 {link} 을(를) 정리했어요.")]
    if unreadable:
        lines.append(unreadable)
    if not requester_found:
        lines.append("(받으실 분을 슬랙에서 찾지 못해 대신 보내요)")
    head = client.chat_postMessage(channel=channel, text="\n\n".join(lines))
    for record in records:
        sent = client.chat_postMessage(channel=head["channel"], thread_ts=head["ts"], text=headline(record), blocks=item_blocks(record))
        # 문서가 갱신돼 사서가 제안을 닫을 때 이 메세지의 버튼을 결과 문구로 바꾼다
        backlog.update(record["backlog_id"], slack_channel=sent["channel"], slack_ts=sent["ts"])
    logger.info("📬 [사서] '%s' 제안 %d건을 %s 에게 보냈습니다.", target["title"], len(records), channel)


# --- [버튼 처리] ---
def apply(client, record: Dict, text: str, user_id: str) -> str:
    """승인된 제안을 노션에 쓰고 백로그를 닫는다. 반영된 노션 주소를 돌려준다.
    쓴 문서는 다시 동기화해 정리·검토가 이어지게 한다 (승인한 사람에게 새 제안이 갈 수 있다)."""
    notion_user = notion_user_for_slack(client, user_id)
    if record["proposal_type"] == backlog.WRITING:
        page = notion_writer.create_proposal_page(record["new_doc_title"], text, notion_user)
        page_id, url = page["page_id"], page["url"]
    else:
        target = record["docs"][0]
        notion_writer.append_decision(target["page_id"], record["content"], text, user_name(client, user_id))
        page_id, url = target["page_id"], target.get("url", "")
    backlog.close(record["backlog_id"], user_id, backlog.RESOLVED, approved_text=text, notion_page_id=page_id, notion_url=url)
    threading.Thread(target=indexing.sync_document_to_db, args=(page_id, config.NOTION_INTEGRATION_TOKEN),
                     kwargs={"requested_by": notion_user}, daemon=True, name="proposal-sync").start()
    return url


def update_item(client, channel_id: str, message: Dict, backlog_id: str, status_text: str) -> None:
    import backlog_slack
    client.chat_update(channel=channel_id, ts=message["ts"], text=message.get("text", ""),
                       blocks=backlog_slack.replace_item_actions(message.get("blocks", []), backlog_id, status_text))


def refuse(client, channel_id: str, user_id: str, result: str) -> bool:
    """처리할 수 없는 상태면 본인에게만 안내하고 True"""
    if result == "ok":
        return False
    text = {"not_assignee": "❌ 담당자만 처리할 수 있어요.", "already": "이미 처리된 제안이에요.",
            "not_found": "⚠️ 삭제되었거나 찾을 수 없는 제안이에요."}[result]
    client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)
    return True


def approve_and_report(client, record: Dict, text: str, user_id: str, channel_id: str, message: Dict) -> None:
    try:
        url = apply(client, record, text, user_id)
    except Exception as e:
        logger.exception("🚨 [사서 제안] 노션 반영 실패: %s", record["content"])
        client.chat_postEphemeral(channel=channel_id, user=user_id,
                                  text=f"❌ 노션에 반영하지 못했어요. 잠시 후 다시 눌러 주세요. ({type(e).__name__}: {str(e)[:200]})")
        return
    where = f"<{url}|노션에 반영했어요>" if url else "노션에 반영했어요"
    update_item(client, channel_id, message, record["backlog_id"], f"✅ 통과 — {where}")


def handle_approve(body, client) -> None:
    user_id, channel_id, backlog_id = body["user"]["id"], body["channel"]["id"], body["actions"][0]["value"]
    if refuse(client, channel_id, user_id, backlog.check_open(backlog_id, user_id)):
        return
    record = backlog.get(backlog_id)
    approve_and_report(client, record, record["proposal"], user_id, channel_id, body["message"])


def handle_hold(body, client) -> None:
    user_id, channel_id, backlog_id = body["user"]["id"], body["channel"]["id"], body["actions"][0]["value"]
    if refuse(client, channel_id, user_id, backlog.check_open(backlog_id, user_id)):
        return
    until = backlog.hold(backlog_id, user_id)
    update_item(client, channel_id, body["message"], backlog_id,
                f"⏸️ 보류했어요. {until}부터 다시 물어볼게요. 그전에 처리하려면 리마인더나 이 메세지의 버튼을 쓰면 돼요.")


def modal_metadata(body) -> str:
    message = body["message"]
    return json.dumps({"backlog_id": body["actions"][0]["value"], "channel": body["channel"]["id"],
                       "ts": message["ts"], "thread_ts": message.get("thread_ts", message["ts"])})


def handle_edit(body, client) -> None:
    """고쳐서 통과: 제안 내용을 채운 입력창을 띄운다"""
    user_id, channel_id, backlog_id = body["user"]["id"], body["channel"]["id"], body["actions"][0]["value"]
    if refuse(client, channel_id, user_id, backlog.check_open(backlog_id, user_id)):
        return
    record = backlog.get(backlog_id)
    client.views_open(trigger_id=body["trigger_id"], view={
        "type": "modal", "callback_id": "proposal_edit_submit", "private_metadata": modal_metadata(body),
        "title": {"type": "plain_text", "text": "고쳐서 통과"},
        "submit": {"type": "plain_text", "text": "통과"}, "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": clip(f"*{record['content']}*", SECTION_CHARS)}},
            {"type": "input", "block_id": "text", "label": {"type": "plain_text", "text": "노션에 쓸 내용"},
             "element": {"type": "plain_text_input", "action_id": "value", "multiline": True, "max_length": INPUT_CHARS,
                         "initial_value": clip(record["proposal"], INPUT_CHARS)}}]})


def handle_reject(body, client) -> None:
    """반려: 사유를 묻는다 (사서가 같은 제안을 다시 내지 않도록 참고한다)"""
    user_id, channel_id, backlog_id = body["user"]["id"], body["channel"]["id"], body["actions"][0]["value"]
    if refuse(client, channel_id, user_id, backlog.check_open(backlog_id, user_id)):
        return
    client.views_open(trigger_id=body["trigger_id"], view={
        "type": "modal", "callback_id": "proposal_reject_submit", "private_metadata": modal_metadata(body),
        "title": {"type": "plain_text", "text": "반려"},
        "submit": {"type": "plain_text", "text": "반려"}, "close": {"type": "plain_text", "text": "취소"},
        "blocks": [{"type": "input", "block_id": "text", "optional": True, "label": {"type": "plain_text", "text": "반려 사유"},
                    "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                                "placeholder": {"type": "plain_text", "text": "예: 폐기된 콘텐츠라 정리할 필요 없음"}}}]})


def submitted(view) -> tuple:
    """(메타데이터, 입력한 글)"""
    meta = json.loads(view["private_metadata"])
    return meta, (view["state"]["values"]["text"]["value"].get("value") or "").strip()


def find_message(client, meta: Dict) -> Dict:
    """모달을 연 메세지 (버튼을 결과 문구로 바꾸기 위해 다시 읽는다)"""
    messages = client.conversations_replies(channel=meta["channel"], ts=meta["thread_ts"]).get("messages", [])
    return next((m for m in messages if m.get("ts") == meta["ts"]), {"ts": meta["ts"], "blocks": []})


def handle_edit_submit(body, client) -> None:
    meta, text = submitted(body["view"])
    user_id = body["user"]["id"]
    if not text or refuse(client, meta["channel"], user_id, backlog.check_open(meta["backlog_id"], user_id)):
        return
    approve_and_report(client, backlog.get(meta["backlog_id"]), text, user_id, meta["channel"], find_message(client, meta))


def handle_reject_submit(body, client) -> None:
    meta, reason = submitted(body["view"])
    user_id = body["user"]["id"]
    if refuse(client, meta["channel"], user_id, backlog.check_open(meta["backlog_id"], user_id)):
        return
    backlog.close(meta["backlog_id"], user_id, backlog.REJECTED, reject_reason=reason)
    update_item(client, meta["channel"], find_message(client, meta), meta["backlog_id"],
                "❌ 반려했어요." + (f" (사유: {reason})" if reason else ""))


# --- [대화로 고치기] ---
# 슬랙 스레드는 한 단계뿐이라, 제안 여러 건이 있는 스레드에서는 답장이 어느 제안에 대한 것인지 알 수 없다.
# 그래서 버튼을 누르면 그 제안만의 새 스레드를 열고, 그 스레드의 메세지를 사서가 고칠 지시로 받는다.
REVISE_PROMPT = (
    "너는 게임 기획팀의 기획서 정리 담당 '사서'다. 네가 낸 [제안]을 담당 기획자가 대화로 함께 고치고 있다. "
    "[기획자의 말]에 따라 제안을 고쳐라. 확인이 필요하면 도구로 기획서를 찾아 읽어라.\n"
    "- 제안의 형식(결정 기록에 붙일 결정 / 지도 문서 초안)과 출처 표기 '(참고: 문서 이름)'는 유지한다.\n"
    "- 기획자가 정한 내용은 그대로 따른다. 기획서와 다르면 고치되 reply에서 짧게 알린다.\n"
    "- 기획자의 말이 질문이면 답만 하고 제안은 바꾸지 않는다.\n"
    "JSON만 출력한다: {\"reply\": \"기획자에게 할 말 (무엇을 바꿨는지 또는 질문에 대한 답) 2문장 이내\", "
    "\"proposal\": \"고친 제안 전체 (바꾸지 않았으면 빈 문자열)\", \"assumptions\": \"근거 없이 정한 부분 요약 (없으면 빈 문자열)\"}"
)


def handle_chat(body, client) -> None:
    """대화로 고치기: 그 제안만의 새 스레드를 열고 지금 제안을 보여준다"""
    user_id, channel_id, backlog_id = body["user"]["id"], body["channel"]["id"], body["actions"][0]["value"]
    if refuse(client, channel_id, user_id, backlog.check_open(backlog_id, user_id)):
        return
    record = backlog.get(backlog_id)
    if record.get("chat_ts"):
        client.chat_postEphemeral(channel=channel_id, user=user_id, text="이미 이 제안의 대화 스레드가 있어요. 그 스레드에서 이어서 말해 주세요.")
        return
    head = client.chat_postMessage(channel=channel_id, text=(
        f"💬 *{headline(record)}*\n이 제안을 대화로 고쳐요. "
        "이 스레드에 고칠 점을 말해 주세요. (예: '보스 부분은 빼 줘', '디고리 내용이 맞는지 다시 확인해 줘')\n"
        "마음에 들면 스레드의 마지막 [✅ 이대로 통과]를 눌러 주세요."))
    client.chat_postMessage(channel=channel_id, thread_ts=head["ts"], text=headline(record), blocks=item_blocks(record))
    backlog.update(backlog_id, chat_ts=head["ts"], chat_history=[])
    update_item(client, channel_id, body["message"], backlog_id, "💬 대화 스레드에서 고치는 중이에요.")


def revise(record: Dict, message: str) -> Dict:
    """사서가 기획자의 말에 따라 제안을 고친다. {"reply", "proposal", "assumptions"}"""
    import review
    request = (f"[제안] {record['content']}\n[문제] {record.get('problem', '')}\n[지금 제안 내용]\n{record['proposal']}\n\n"
               f"[이 스레드의 이전 대화]\n{librarian_slack.history_text(record.get('chat_history', []))}\n\n[기획자의 말]\n{message}")
    data = review.parse_json(review.run_with_tools(REVISE_PROMPT, request, f"'{record['content']}' 대화로 고치기",
                                                   librarian_report.REVISE))
    return {"reply": data.get("reply") or "고쳐 봤어요.", "proposal": (data.get("proposal") or "").strip(),
            "assumptions": data.get("assumptions", "")}


def handle_chat_message(client, event) -> bool:
    """DM 스레드 메세지가 '대화로 고치기' 스레드의 것이면 처리하고 True. 아니면 False (일반 질문으로 처리)"""
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return False
    record = backlog.proposal_by_chat(thread_ts)
    if record is None:
        return False
    channel_id, user_id = event["channel"], event.get("user")
    if record["status"] != backlog.OPEN or record["assignee_id"] != user_id:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                text="이미 처리된 제안이에요." if record["status"] != backlog.OPEN else "담당자만 고칠 수 있어요.")
        return True
    message = event.get("text", "")
    turn = librarian_slack.chat_turn(client, channel_id, thread_ts, record["content"], record.get("chat_history", []), message,
                                     lambda: revise(record, message))
    if turn is None:
        return True
    result, history, placeholder_ts = turn
    fields = {"chat_history": history}
    if result["proposal"]:
        fields.update(proposal=result["proposal"], assumptions=result["assumptions"])
    backlog.update(record["backlog_id"], **fields)
    record.update(fields)
    if result["proposal"]:
        client.chat_update(channel=channel_id, ts=placeholder_ts, text=result["reply"],
                           blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"✏️ {result['reply']}"}}] + item_blocks(record))
    else:
        client.chat_update(channel=channel_id, ts=placeholder_ts, text=f"💬 {result['reply']}")
    return True


# --- [토스: 다른 담당자에게 넘기기] ---
def handle_toss(body, client) -> None:
    """토스: 넘길 사람과 한마디를 묻는다"""
    user_id, channel_id, backlog_id = body["user"]["id"], body["channel"]["id"], body["actions"][0]["value"]
    if refuse(client, channel_id, user_id, backlog.check_open(backlog_id, user_id)):
        return
    record = backlog.get(backlog_id)
    client.views_open(trigger_id=body["trigger_id"], view=librarian_slack.toss_view(
        "proposal_toss_submit", clip(f"*{record['content']}*", SECTION_CHARS), modal_metadata(body)))


def handle_toss_submit(body, client) -> None:
    """담당자를 바꾸고, 새 담당자에게 제안을 보내고, 원래 메세지에 넘겼다고 남긴다.
    대화로 고치던 중이었으면 그 대화 기록은 넘어가고, 새 담당자는 자기 DM에서 새로 대화 스레드를 연다."""
    meta, to_user, memo = librarian_slack.toss_input(body["view"])
    user_id = body["user"]["id"]
    if not to_user or to_user == user_id or refuse(client, meta["channel"], user_id, backlog.check_open(meta["backlog_id"], user_id)):
        return
    record = backlog.get(meta["backlog_id"])
    if record.get("chat_ts"):
        client.chat_postMessage(channel=meta["channel"], thread_ts=record["chat_ts"],
                                text=f"🔁 <@{to_user}> 님에게 넘겼어요. 이 스레드는 더 이상 쓰지 않아요.")
    fields = {"assignee_id": to_user, "assignee_name": user_name(client, to_user), "chat_ts": "",
              "toss_history": librarian_slack.toss_history(record, user_id, to_user, memo)}
    backlog.update(record["backlog_id"], **fields)
    record.update(fields)

    head = client.chat_postMessage(channel=to_user, text=(
        librarian_slack.toss_notice(user_id, "사서 제안을", memo)
        + "\n스레드에서 처리해 주세요. 담당이 아니면 다시 [🔁 토스]로 넘길 수 있어요."))
    sent = client.chat_postMessage(channel=head["channel"], thread_ts=head["ts"], text=headline(record), blocks=item_blocks(record))
    backlog.update(record["backlog_id"], slack_channel=sent["channel"], slack_ts=sent["ts"])
    update_item(client, meta["channel"], find_message(client, meta), record["backlog_id"], f"🔁 <@{to_user}> 님에게 넘겼어요.")
    logger.info("🔁 [사서 제안] %s → %s: %s", user_id, to_user, record["content"])
