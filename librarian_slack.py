"""사서가 슬랙에서 담당자와 주고받는 공통 흐름: 대화로 고치기 한 턴, 토스 입력창과 기록.
사서 제안(proposal_slack.py)과 기획서 속성 검사(property_slack.py)가 같이 쓴다. 무엇을 고치고 어디에 저장할지는 각 모듈이 정한다."""
import json
import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple
import backlog

logger = logging.getLogger(__name__)

# 사서에게 함께 넘기는 대화 스레드의 이전 대화 수
CHAT_HISTORY_TURNS = 10


# --- [대화로 고치기] ---
def history_text(history: List[Dict]) -> str:
    """사서에게 넘길 이전 대화 (최근 CHAT_HISTORY_TURNS개)"""
    return "\n".join(f"{h['role']}: {h['text']}" for h in history[-CHAT_HISTORY_TURNS:]) or "(없음)"


def chat_turn(client, channel_id: str, thread_ts: str, label: str, history: List[Dict], message: str,
              revise: Callable[[], Dict]) -> Optional[Tuple[Dict, List[Dict], str]]:
    """'고치는 중' 메세지를 띄우고 revise()로 사서가 고치게 한다. revise는 {"reply", ...}를 돌려준다.
    (결과, 이번 말까지 더한 대화 기록, '고치는 중' 메세지 ts). 실패하면 그 메세지에 알리고 None"""
    placeholder = client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text="🧹 사서가 고치는 중…")
    try:
        result = revise()
    except Exception as e:
        logger.exception("🚨 [사서] 대화로 고치기 실패: %s", label)
        client.chat_update(channel=channel_id, ts=placeholder["ts"], text=f"❌ 고치지 못했어요. 다시 말해 주세요. ({type(e).__name__})")
        return None
    history = history + [{"role": "기획자", "text": message}, {"role": "사서", "text": result["reply"]}]
    return result, history, placeholder["ts"]


# --- [토스: 다른 담당자에게 넘기기] ---
def toss_view(callback_id: str, title: str, private_metadata: str) -> Dict:
    """넘길 사람과 한마디를 묻는 입력창. title은 무엇을 넘기는지 보여주는 글"""
    return {
        "type": "modal", "callback_id": callback_id, "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "토스"},
        "submit": {"type": "plain_text", "text": "넘기기"}, "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": title}},
            {"type": "input", "block_id": "to", "label": {"type": "plain_text", "text": "넘길 사람"},
             "element": {"type": "users_select", "action_id": "value"}},
            {"type": "input", "block_id": "memo", "optional": True, "label": {"type": "plain_text", "text": "한마디"},
             "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                         "placeholder": {"type": "plain_text", "text": "예: 전투 담당이 판단할 내용이라 넘겨요"}}}]}


def toss_input(view: Dict) -> Tuple[Dict, Optional[str], str]:
    """토스 입력창에서 (메타데이터, 넘길 사람, 한마디)"""
    values = view["state"]["values"]
    return (json.loads(view["private_metadata"]), values["to"]["value"].get("selected_user"),
            (values["memo"]["value"].get("value") or "").strip())


def toss_history(record: Dict, from_user: str, to_user: str, memo: str) -> List[Dict]:
    """기록의 토스 이력에 이번 토스를 더한 것"""
    return record.get("toss_history", []) + [{"from": from_user, "to": to_user, "memo": memo,
                                              "at": datetime.now(backlog.KST).isoformat(timespec="seconds")}]


def toss_notice(from_user: str, what: str, memo: str) -> str:
    """새 담당자에게 보내는 첫 줄: 누가 무엇을 넘겼는지와 한마디. what은 조사까지 (예: '사서 제안을')"""
    return f"🔁 <@{from_user}> 님이 {what} 넘겼어요." + (f"\n> {memo}" if memo else "")
