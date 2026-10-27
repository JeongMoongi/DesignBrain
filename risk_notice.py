"""'DB 등록'한 기획서에서 QA가 더 세심히 볼 것을 QA 담당자(TEST_NOTIFY_IDS)에게 DM으로 알린다. 버튼 없이 DM 한 통이다.
더 자세한 건 QA 담당자가 슬랙에서 @Brain에게 편하게 물어보면 된다 (질문 답변 에이전트가 QA 관점으로 답한다: qa_lens.py).

DM은 QA가 '무엇을 해야 하는지'가 바로 보이게 세 덩어리로 나눈다 (해당하는 것만 보낸다).
- ❓ 미정의: 문서에 처리 기준이 없어서 QA가 정답을 알 수 없는 것. 테스트 전에 기획에 먼저 물어본다. 문의할 사람(문서 담당자)을 함께 적는다.
- 🎯 바로 테스트할 것: 문서에 식·수치·예시가 있어 정답이 정해진 것. '해보기 → 정상 기준'. 재미·밸런스처럼 정답이 없는 것은 '→ 비교 기준'.
- 🎭 이 관점으로 보세요: 입문 유저 기준, 디고리와 비교처럼 그 관점으로 플레이할 때 판단이 달라질 때만. 추상적이거나 개발자 관점이면 쓰지 않는다.

- 처음 등록한 문서는 알린다. 이미 알린 문서는 줄(문단)의 TEST_NOTICE_CHANGE_RATIO(기본 30%) 이상이 바뀌었을 때만 다시 알린다.
  자잘한 수정은 알리지 않고, 비교 기준은 마지막으로 알린 본문이라 자잘한 수정이 쌓여 많이 바뀌면 그때 알린다.
- 알릴 만한 것이 없으면 보내지 않는다.
- 분석에 시간이 걸려 동기화를 붙잡지 않도록 별도 스레드에서 돌리고, 실패해도 동기화에는 영향이 없다.
"""
import hashlib
import logging
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from qdrant_client.models import PointStruct
import backlog
import clients
import config
import notion_parser
import notion_writer
import qa_lens
import review

logger = logging.getLogger(__name__)

DOC_CHARS = 30000
MAX_UNKNOWN, MAX_TESTS, MAX_VIEWS = 3, 3, 2
# 슬랙에서 훑어 읽는 글이라 항목마다 길이를 제한한다 (프롬프트가 짧게 쓰게 하지만 넘치면 …로 자른다)
UNKNOWN_CHARS, TEST_NAME_CHARS, TEST_CHARS, VIEW_CHARS = 60, 20, 100, 70
LEVELS = ("높음", "보통", "낮음")
LEVEL_EMOJI = {"높음": "🔴", "보통": "🟠", "낮음": "🟢"}

PROMPT = (
    "너는 게임 QA 팀의 테스트 설계 담당이다. 방금 DB에 등록된 기획서를 읽고, QA가 이 문서를 테스트할 때 무엇을 해야 하는지 미리 알려라.\n\n"
    "[살펴볼 위험 신호]\n"
    "- 수치·공식이 많거나 곱/합 연산, 중첩이 있다\n"
    "- 상태가 바뀌는 흐름(진입·유지·해제, 지속시간, 중첩)이 있다\n"
    "- 다른 영웅·아이템·각인·시스템을 참조하거나 동시에 발동할 수 있다\n"
    "- 조건 분기가 많거나, 예외 상황의 처리가 적혀 있지 않다\n"
    "- '미정'·'TBD'·수치 없음처럼 모호하거나 비어 있는 서술이 있다\n"
    "- 새로 도입한 개념·용어라 기존 문서에 근거가 없다\n"
    "- 강약이 극단으로 갈 수 있는 수치나 성장 구조가 있다\n\n"
    "[세 덩어리로 나눠 써라. 해당하는 것만 쓰고, 없으면 빈 배열로 둬라]\n"
    "1) unknown (❓ 미정의, 최대 3개): 문서에 처리 기준이 없어서 QA가 정답을 알 수 없는 것. 기획에 먼저 물어봐야 하는 것들이다. "
    "추상적인 이름('부호·0 경계') 대신 구체적인 상황을 한 문장으로 써라. 예: '한 축만 0일 때 어떻게 밀리는지'. 40자 안팎, 60자를 넘기지 마라.\n"
    "2) tests (🎯 바로 테스트할 것, 최대 3개): 문서에 식·수치·예시·조건이 적혀 있어 정답이 정해진 것만. "
    "name은 15자 안팎의 이름, test는 '해보기 → 정상 기준' 한 줄(60자 안팎, 100자 넘기지 않기)이다. "
    "예: '`(-1, 5)`에 `(3, 3)` 적용 → `(-4, 8)`'. 문서의 수치는 `코드`로 인용하라. " + qa_lens.JUDGE_RULE + "\n"
    "3) views (🎭 이 관점으로 보세요, 최대 2개): " + qa_lens.VIEW_RULE + " 한 줄은 70자 안팎으로 써라.\n\n"
    "[규칙]\n"
    "- 문서에 실제로 적힌 내용에서 찾고, 없는 내용을 지어내지 마라. 문서에 정답이 적혀 있지 않은 것을 tests에 넣지 말고 unknown에 넣어라.\n"
    "- 읽는 사람은 슬랙에서 훑어 보는 QA다. 문장을 짧게, 한 줄에 한 가지만 써라.\n"
    "- 알릴 만한 것이 하나도 없으면 세 배열을 모두 비워라.\n\n"
    '[출력] JSON 하나만: {"level": "높음|보통|낮음", "unknown": ["상황 한 문장"], "tests": [{"name": "", "test": ""}], "views": ["관점 한 줄"]}'
)


def clean(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def shorten(value, limit: int) -> str:
    """limit을 넘으면 말줄임표로 자른다"""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def short_list(values, limit: int, count: int) -> List[str]:
    return [t for t in (shorten(v, limit) for v in (values if isinstance(values, list) else []) if isinstance(v, str)) if t][:count]


def analyze(doc_data: Dict) -> Dict:
    """등록된 문서에서 QA가 할 것 {"level", "unknown": [..], "tests": [{"name", "test"}], "views": [..]}.
    unknown = 문서에 없어서 기획에 물어볼 것, tests = 정답이 정해져 바로 해볼 것(해보기 → 정상 기준), views = 이 관점으로 볼 것"""
    response = clients.get_openai().responses.create(
        model=config.REFINE_MODEL, instructions=PROMPT, reasoning={"effort": config.REVIEW_REASONING_EFFORT},
        input=f"[문서 제목] {doc_data.get('title', '')}\n[노션 위치] {' > '.join(doc_data.get('path', [])) or '(최상위)'}\n\n"
              f"[본문]\n{doc_data.get('full_text', '')[:DOC_CHARS]}")
    data = review.parse_json(response.output_text or "")
    tests = []
    for raw in data.get("tests", []) if isinstance(data.get("tests"), list) else []:
        if isinstance(raw, dict) and clean(raw.get("test"), 10):
            tests.append({"name": shorten(raw.get("name"), TEST_NAME_CHARS), "test": shorten(raw.get("test"), TEST_CHARS)})
    level = clean(data.get("level"), 4)
    return {"level": level if level in LEVELS else "보통", "unknown": short_list(data.get("unknown"), UNKNOWN_CHARS, MAX_UNKNOWN),
            "tests": tests[:MAX_TESTS], "views": short_list(data.get("views"), VIEW_CHARS, MAX_VIEWS)}


def has_content(risks: Dict) -> bool:
    return bool(risks["unknown"] or risks["tests"] or risks["views"])


# --- [얼마나 바뀌었나] ---
def fingerprint(text: str) -> List[str]:
    """본문 줄마다의 짧은 지문 (공백 차이는 무시). 지난번과 얼마나 달라졌는지 재는 데 쓴다"""
    return sorted({hashlib.sha1(" ".join(line.split()).encode("utf-8")).hexdigest()[:8] for line in text.splitlines() if line.strip()})


def change_ratio(old: List[str], new: List[str]) -> float:
    """0(같음) ~ 1(전부 다름): 지난번 본문의 줄 수 대비 바뀐 줄의 비율.
    고친 줄은 빠진 줄과 새 줄이 하나씩 생기므로 빠진 줄 수와 새 줄 수 중 큰 쪽을 센다 (20줄 중 4줄 수정·추가·삭제 = 20%)"""
    old_set, new_set = set(old), set(new)
    if not old_set:
        return 1.0 if new_set else 0.0
    return min(1.0, max(len(old_set - new_set), len(new_set - old_set)) / len(old_set))


# --- [마지막으로 알린 본문 기록: 문서마다 하나] ---
def notice_id(page_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-notice:{page_id}"))


def get_notice(page_id: str) -> Optional[Dict]:
    points = clients.get_qdrant().retrieve(collection_name=config.TEST_NOTICE_COLLECTION, ids=[notice_id(page_id)])
    return points[0].payload if points else None


def save_notice(page_id: str, title: str, lines: List[str], notified: bool) -> None:
    clients.get_qdrant().upsert(collection_name=config.TEST_NOTICE_COLLECTION, points=[PointStruct(
        id=notice_id(page_id), vector={}, payload={"page_id": page_id, "title": title, "lines": lines, "notified": notified,
                                                   "checked_at": datetime.now(backlog.KST).isoformat()})])


# --- [알림] ---
def message_text(doc_data: Dict, risks: Dict, change: Optional[float] = None, contact: str = "") -> str:
    """훑어 읽기 쉽게: 머리줄 / ❓ 미정의 / 🎯 바로 테스트할 것 / 🎭 이 관점으로 보세요 (비어 있는 덩어리는 빼고).
    change는 지난번 알린 뒤 바뀐 비율 (None이면 처음 등록), contact는 미정의를 물어볼 사람"""
    title = doc_data.get("title") or "제목 없음"
    link = f"<{doc_data['url']}|{title}>" if doc_data.get("url") else title
    state = "🆕 처음 등록" if change is None else f"🔄 {round(change * 100)}% 수정"
    lines = [f"🔎 {link} 등록됐어요 ({LEVEL_EMOJI[risks['level']]} {risks['level']}) · {state}"]
    if risks["unknown"]:
        lines.append(f"\n❓ *미정의* — 기획에 먼저 물어보세요" + (f" (문의: {contact})" if contact else ""))
        lines += [f"• {u}" for u in risks["unknown"]]
    if risks["tests"]:
        lines.append("\n🎯 *바로 테스트할 것*")
        lines += [f"{i}. {t['name']} — {t['test']}" if t["name"] else f"{i}. {t['test']}" for i, t in enumerate(risks["tests"], 1)]
    if risks["views"]:
        lines.append("\n🎭 *이 관점으로 보세요*")
        lines += [f"• {v}" for v in risks["views"]]
    lines.append(f"\n💬 더 궁금하면 `@Brain {title} QA 뭘 볼까?`")
    return "\n".join(lines)


def find_contact(client, page_id: str, requested_by: Optional[str]) -> Dict[str, str]:
    """미정의를 물어볼 사람 {"name", "slack_id"}: 문서의 담당자 속성, 비어 있으면 'DB 등록'을 누른 사람. 찾지 못하면 빈 dict.
    찾는 데 실패해도 알림은 그대로 보낸다 (문의할 사람만 빠진다)"""
    import proposal_slack
    people = []
    if config.NOTION_INTEGRATION_TOKEN:
        try:
            prop = (notion_parser.fetch_page(page_id, config.NOTION_INTEGRATION_TOKEN) or {}).get("properties", {}).get(config.PROPERTY_OWNER, {})
            people = prop.get("people", []) if prop.get("type") == "people" else []
        except Exception as e:
            logger.warning("⚠️ [테스트 위험 알림] 문서 담당자를 읽지 못했습니다: %s", e)
    candidate = people[0] if people else ({"id": requested_by} if requested_by else None)
    if not candidate or not candidate.get("id"):
        return {}
    found = {"name": candidate.get("name") or "", "slack_id": ""}
    try:
        found["slack_id"] = proposal_slack.slack_user_for_notion(client, candidate["id"]) or ""
        found["name"] = found["name"] or notion_writer.user_info(candidate["id"]).get("name", "")
    except Exception as e:
        logger.warning("⚠️ [테스트 위험 알림] 문의할 사람을 슬랙에서 찾지 못했습니다: %s", e)
    return found if found["name"] or found["slack_id"] else {}


def contact_text(contact: Dict[str, str], recipient: str) -> str:
    """DM(사용자 ID)에는 슬랙 멘션으로, 채널에는 멘션이 담당자를 호출하지 않게 이름으로 쓴다"""
    mention = f"<@{contact['slack_id']}>" if contact.get("slack_id") else ""
    if recipient.startswith(("U", "W")):
        return mention or contact.get("name", "")
    return contact.get("name") or mention


# 위험 영역을 분석 중인 문서 (기획자가 'DB 등록'을 연달아 눌러도 같은 알림이 두 번 가지 않게)
_analyzing = set()
_analyzing_lock = threading.Lock()


def notify(doc_data: Dict, requested_by: Optional[str] = None) -> bool:
    """QA 담당자들에게 DM으로 알린다. 보냈으면 True. requested_by는 'DB 등록'을 누른 노션 사용자 (문서에 담당자가 없을 때 문의할 사람)"""
    if not config.TEST_NOTIFY_IDS:
        return False
    page_id = doc_data["page_id"]
    with _analyzing_lock:
        if page_id in _analyzing:
            return False
        _analyzing.add(page_id)
    try:
        return _notify(doc_data, requested_by)
    finally:
        with _analyzing_lock:
            _analyzing.discard(page_id)


def _notify(doc_data: Dict, requested_by: Optional[str]) -> bool:
    import proposal_slack
    page_id, title = doc_data["page_id"], doc_data.get("title", "")
    lines = fingerprint(doc_data.get("full_text", ""))
    previous, ratio = get_notice(page_id), None
    if previous:
        ratio = change_ratio(previous.get("lines", []), lines)
        if ratio < config.TEST_NOTICE_CHANGE_RATIO:
            logger.info("ℹ️ [테스트 위험 알림] '%s': 지난번 알린 뒤 %.0f%%만 바뀌어 다시 알리지 않습니다.", title, ratio * 100)
            return False
    client = proposal_slack.web_client()
    if client is None:
        logger.info("ℹ️ [테스트 위험 알림] 슬랙 토큰이 없어 '%s' 알림을 보내지 않습니다.", title)
        return False
    risks = analyze(doc_data)
    if not has_content(risks):
        save_notice(page_id, title, lines, notified=False)
        logger.info("ℹ️ [테스트 위험 알림] '%s': 알릴 만한 것이 없어 보내지 않습니다.", title)
        return False
    contact = find_contact(client, page_id, requested_by) if risks["unknown"] else {}
    sent = 0
    for recipient in config.TEST_NOTIFY_IDS:
        try:
            client.chat_postMessage(channel=recipient, text=message_text(doc_data, risks, ratio, contact_text(contact, recipient)))
            sent += 1
        except Exception:
            logger.exception("🚨 [테스트 위험 알림] %s 에게 보내지 못했습니다 ('%s')", recipient, title)
    if sent:
        # 한 명에게도 못 보냈으면 기록하지 않아 다음 'DB 등록' 때 다시 시도한다
        save_notice(page_id, title, lines, notified=True)
        logger.info("📬 [테스트 위험 알림] '%s' 미정의 %d / 바로 테스트 %d / 관점 %d 을(를) %d명에게 알렸습니다.",
                    title, len(risks["unknown"]), len(risks["tests"]), len(risks["views"]), sent)
    return bool(sent)


def notify_async(doc_data: Dict, requested_by: Optional[str] = None) -> None:
    """동기화를 붙잡지 않게 별도 스레드에서 알린다. 받을 사람이 설정돼 있지 않으면 스레드도 만들지 않는다"""
    if not config.TEST_NOTIFY_IDS:
        return

    def work():
        try:
            notify(doc_data, requested_by)
        except Exception:
            logger.exception("🚨 [테스트 위험 알림] 실패 (page_id: %s)", doc_data.get("page_id"))

    threading.Thread(target=work, daemon=True, name="risk-notice").start()
