from datetime import datetime, timedelta
import backlog
import librarian_report

UNTIL = datetime(2026, 10, 26, 10, tzinfo=backlog.KST)
SINCE = UNTIL - timedelta(days=7)
IN_WEEK = (UNTIL - timedelta(days=2)).isoformat()
LAST_WEEK = (SINCE - timedelta(days=3)).isoformat()


def proposal(status, created_at=IN_WEEK, **fields):
    return {"kind": backlog.PROPOSAL, "proposal_type": backlog.DECISION, "status": status, "created_at": created_at,
            "content": "결정이 필요함: 진흙사냥개 상태", "proposal": "원안", **fields}


def usage(task, inp, out, at=UNTIL - timedelta(days=1)):
    return {"task": task, "title": "문서", "input_tokens": inp, "output_tokens": out, "at_ts": at.timestamp()}


def test_weekly_section_counts_this_weeks_proposals_and_tokens():
    proposals = [
        proposal(backlog.OPEN, proposal_type=backlog.WRITING),
        proposal(backlog.RESOLVED, resolved_at=IN_WEEK, approved_text="원안"),
        proposal(backlog.RESOLVED, resolved_at=IN_WEEK, approved_text="고친 안"),
        proposal(backlog.RESOLVED, resolved_at=IN_WEEK, approved_text="원안", chat_history=[{"role": "user", "text": "고쳐줘"}]),
        proposal(backlog.REJECTED, resolved_at=IN_WEEK, reject_reason="이미 폐기된 기획"),
        proposal(backlog.APPLIED, resolved_at=IN_WEEK),
        # 지난주에 만들어 이번 주에 보류한 제안 (보류는 다시 물을 날짜만 저장한다)
        proposal(backlog.HELD, created_at=LAST_WEEK,
                 held_until=(UNTIL - timedelta(days=1) + timedelta(days=backlog.HOLD_DAYS)).isoformat()),
        # 지난주에 처리한 제안은 처리 수에 넣지 않는다
        proposal(backlog.REJECTED, created_at=LAST_WEEK, resolved_at=LAST_WEEK, reject_reason="지난주 사유"),
    ]
    records = [usage("검토", 20000, 2000), usage("정리", 1000, 500), usage("정리", 3000, 700),
               usage("종합", 999, 9, at=SINCE - timedelta(hours=1))]
    text = librarian_report.weekly_section(proposals, records, SINCE, UNTIL)

    assert "새 제안 6건 (결정 5 · 작성 1)" in text
    assert "처리: 승인 3 (고쳐서 2) · 보류 1 · 반려 1 · 문서 갱신으로 닫힘 1" in text
    assert "남은 제안: 대기 1 · 보류 1" in text
    assert "• 결정이 필요함: 진흙사냥개 상태 — 이미 폐기된 기획" in text and "지난주 사유" not in text
    # 작업은 정해진 순서로, 지난주 기록은 빼고 합친다
    assert text.index("• 정리 2회: 4,000 / 1,200") < text.index("• 검토 1회: 20,000 / 2,000")
    assert "종합" not in text
    assert "합계: 24,000 / 3,200" in text


def test_weekly_section_is_empty_without_activity():
    assert librarian_report.weekly_section([proposal(backlog.OPEN, created_at=LAST_WEEK)], [], SINCE, UNTIL) == ""


def test_weekly_report_appends_librarian_activity(local_db, monkeypatch):
    import config
    import qa_slack
    monkeypatch.setattr(config, "QA_REPORT_USER_ID", "U1")
    librarian_report.record_usage(librarian_report.REVIEW, "문서", 10, 2)
    sent = []
    client = type("Client", (), {"chat_postMessage": lambda self, **kw: sent.append(kw)})()
    qa_slack.send_weekly_report(client)
    assert "질문이 없었어요" in sent[0]["text"] and "🧹 *사서 활동*" in sent[0]["text"]
    assert "• 검토 1회: 10 / 2" in sent[0]["text"]
