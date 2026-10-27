"""'DB 등록'한 기획서에서 QA가 더 세심히 볼 영역을 QA 담당자(TEST_NOTIFY_IDS)에게 DM으로 알린다. 버튼 없이 DM 한 통이다.
더 자세한 건 QA 담당자가 슬랙에서 @Brain에게 편하게 물어보면 된다 (질문 답변 에이전트가 QA 관점으로 답한다: qa_lens.py).

- 처음 등록한 문서는 알린다. 이미 알린 문서는 줄(문단)의 TEST_NOTICE_CHANGE_RATIO(기본 30%) 이상이 바뀌었을 때만 다시 알린다.
  자잘한 수정은 알리지 않고, 비교 기준은 마지막으로 알린 본문이라 자잘한 수정이 쌓여 많이 바뀌면 그때 알린다.
- 알릴 만한 위험이 없으면 보내지 않는다.
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
import qa_lens
import review

logger = logging.getLogger(__name__)

DOC_CHARS = 30000
MAX_AREAS = 5
LEVELS = ("높음", "보통", "낮음")
LEVEL_EMOJI = {"높음": "🔴", "보통": "🟠", "낮음": "🟢"}

PROMPT = (
    "너는 게임 QA 팀의 테스트 설계 담당이다. 방금 DB에 등록된 기획서를 읽고, QA가 이 문서를 테스트할 때 특히 세심하게 볼 영역을 미리 알려라.\n\n"
    "[위험 신호]\n"
    "- 수치·공식이 많거나 곱/합 연산, 중첩이 있다\n"
    "- 상태가 바뀌는 흐름(진입·유지·해제, 지속시간, 중첩)이 있다\n"
    "- 다른 영웅·아이템·각인·시스템을 참조하거나 동시에 발동할 수 있다\n"
    "- 조건 분기가 많거나, 예외 상황의 처리가 적혀 있지 않다\n"
    "- '미정'·'TBD'·수치 없음처럼 모호하거나 비어 있는 서술이 있다\n"
    "- 새로 도입한 개념·용어라 기존 문서에 근거가 없다\n"
    "- 연출·애니메이션의 타이밍이 동작과 맞물린다\n"
    "- 강약이 극단으로 갈 수 있는 수치나 성장 구조가 있다\n\n"
    "[규칙]\n"
    "- 문서에 실제로 적힌 내용에서 위험을 찾고, 없는 내용을 지어내지 마라. 근거로 항목 이름이나 수치를 인용하라.\n"
    "- 위험이 큰 순서로 최대 5개. 뚜렷한 위험이 없으면 areas를 빈 배열로 둬라.\n"
    "- lens에는 테스트할 관점의 키를 써라: " + ", ".join(f"{l.key}({l.name})" for l in qa_lens.LENSES) + "\n\n"
    '[출력] JSON 하나만: {"level": "높음|보통|낮음", "areas": [{"area": "영역 이름(짧게)", "reason": "문서의 어떤 내용 때문에 위험한지 한두 문장", '
    '"lens": ["관점 키"], "check": "테스트 때 확인할 것 한 줄"}]}'
)


def clean(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def analyze(doc_data: Dict) -> Dict:
    """등록된 문서에서 세심히 볼 영역 {"level", "areas": [{"area", "reason", "lens", "check"}]}"""
    response = clients.get_openai().responses.create(
        model=config.REFINE_MODEL, instructions=PROMPT, reasoning={"effort": config.REVIEW_REASONING_EFFORT},
        input=f"[문서 제목] {doc_data.get('title', '')}\n[노션 위치] {' > '.join(doc_data.get('path', [])) or '(최상위)'}\n\n"
              f"[본문]\n{doc_data.get('full_text', '')[:DOC_CHARS]}")
    data = review.parse_json(response.output_text or "")
    areas = []
    for raw in data.get("areas", []):
        if not isinstance(raw, dict) or not clean(raw.get("area"), 80):
            continue
        keys = [k for k in (raw.get("lens") or []) if isinstance(k, str) and k in qa_lens.LENS_BY_KEY]
        areas.append({"area": clean(raw.get("area"), 80), "reason": clean(raw.get("reason"), 400),
                      "lens": list(dict.fromkeys(keys)), "check": clean(raw.get("check"), 300)})
    level = clean(data.get("level"), 4)
    return {"level": level if level in LEVELS else "보통", "areas": areas[:MAX_AREAS]}


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
def message_text(doc_data: Dict, risks: Dict) -> str:
    title = doc_data.get("title") or "제목 없음"
    link = f"<{doc_data['url']}|{title}>" if doc_data.get("url") else title
    lines = [f"🔎 {link} 기획서가 DB에 등록됐어요. 테스트할 때 특히 볼 영역이에요. (위험도 {LEVEL_EMOJI[risks['level']]} {risks['level']})"]
    for i, area in enumerate(risks["areas"], 1):
        lines.append(f"\n*{i}. {area['area']}*\n{area['reason']}")
        if area["check"]:
            lines.append(f"↳ 확인: {area['check']}")
        if area["lens"]:
            lines.append(f"↳ 관점: {', '.join(qa_lens.LENS_BY_KEY[k].name for k in area['lens'])}")
    lines.append(f"\n더 궁금하면 `@Brain {title} QA 할건데 뭘 볼까?`처럼 편하게 물어보세요.")
    return "\n".join(lines)


# 위험 영역을 분석 중인 문서 (기획자가 'DB 등록'을 연달아 눌러도 같은 알림이 두 번 가지 않게)
_analyzing = set()
_analyzing_lock = threading.Lock()


def notify(doc_data: Dict) -> bool:
    """QA 담당자들에게 DM으로 알린다. 보냈으면 True"""
    if not config.TEST_NOTIFY_IDS:
        return False
    page_id = doc_data["page_id"]
    with _analyzing_lock:
        if page_id in _analyzing:
            return False
        _analyzing.add(page_id)
    try:
        return _notify(doc_data)
    finally:
        with _analyzing_lock:
            _analyzing.discard(page_id)


def _notify(doc_data: Dict) -> bool:
    import proposal_slack
    page_id, title = doc_data["page_id"], doc_data.get("title", "")
    lines = fingerprint(doc_data.get("full_text", ""))
    previous = get_notice(page_id)
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
    if not risks["areas"]:
        save_notice(page_id, title, lines, notified=False)
        logger.info("ℹ️ [테스트 위험 알림] '%s': 뚜렷한 위험 영역이 없어 알리지 않습니다.", title)
        return False
    text, sent = message_text(doc_data, risks), 0
    for recipient in config.TEST_NOTIFY_IDS:
        try:
            client.chat_postMessage(channel=recipient, text=text)
            sent += 1
        except Exception:
            logger.exception("🚨 [테스트 위험 알림] %s 에게 보내지 못했습니다 ('%s')", recipient, title)
    if sent:
        # 한 명에게도 못 보냈으면 기록하지 않아 다음 'DB 등록' 때 다시 시도한다
        save_notice(page_id, title, lines, notified=True)
        logger.info("📬 [테스트 위험 알림] '%s' 위험 영역 %d개를 %d명에게 알렸습니다.", title, len(risks["areas"]), sent)
    return bool(sent)


def notify_async(doc_data: Dict) -> None:
    """동기화를 붙잡지 않게 별도 스레드에서 알린다. 받을 사람이 설정돼 있지 않으면 스레드도 만들지 않는다"""
    if not config.TEST_NOTIFY_IDS:
        return

    def work():
        try:
            notify(doc_data)
        except Exception:
            logger.exception("🚨 [테스트 위험 알림] 실패 (page_id: %s)", doc_data.get("page_id"))

    threading.Thread(target=work, daemon=True, name="risk-notice").start()
