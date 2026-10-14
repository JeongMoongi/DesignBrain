"""질문 기록을 엑셀에서 열 수 있는 CSV로 내려받는다.

    python export_qa.py                  # 전체 기록
    python export_qa.py --days 7         # 최근 7일
    python export_qa.py --out 기록.csv   # 파일 이름 지정 (기본: qa_logs_날짜.csv)
    python export_qa.py --open           # 만든 뒤 엑셀로 바로 열기 (Windows, export_qa.bat이 사용)

PC에서 실행해도 같은 Qdrant Cloud를 읽으므로 VM에 접속하지 않아도 된다.
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timedelta
import backlog
import qa_log

COLUMNS = [
    ("asked_at", "질문 시각"), ("user_name", "질문한 사람"), ("channel_type", "채널/DM"), ("follow_up", "후속 질문"),
    ("question", "질문"), ("search_query", "검색에 쓴 질문"), ("status", "결과"), ("error_stage", "실패 단계"),
    ("error_reason", "실패 원인"), ("used_docs", "사용한 기획서"), ("up", "👍"), ("down", "👎"),
    ("image_count", "첨부 도판 수"), ("answer", "답변"), ("user_id", "사용자 ID"), ("answer_ts", "답변 메세지 ts"),
]


def row(entry):
    values = {**entry,
              "used_docs": " | ".join(entry.get("used_docs", [])),
              "up": qa_log.feedback_count(entry, qa_log.UP),
              "down": qa_log.feedback_count(entry, qa_log.DOWN),
              "follow_up": "예" if entry.get("follow_up") else "",
              "status": "답변" if entry.get("status") == "success" else "실패"}
    return [values.get(key) if values.get(key) is not None else "" for key, _ in COLUMNS]


def export(path, days=None):
    now = datetime.now(backlog.KST)
    records = qa_log.entries(since=now - timedelta(days=days) if days else None)
    # 엑셀이 한글을 바로 읽도록 BOM이 붙은 UTF-8로 저장한다
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([label for _, label in COLUMNS])
        writer.writerows(row(e) for e in records)
    return len(records)


def main(args):
    parser = argparse.ArgumentParser(description="질문 기록을 CSV로 내려받는다")
    parser.add_argument("--days", type=int, help="최근 며칠 기록만 (생략하면 전체)")
    parser.add_argument("--out", help="저장할 파일 이름")
    parser.add_argument("--open", action="store_true", help="만든 뒤 기본 프로그램(엑셀)으로 열기")
    opts = parser.parse_args(args)
    path = opts.out or f"qa_logs_{datetime.now(backlog.KST):%Y%m%d_%H%M}.csv"
    count = export(path, opts.days)
    print(f"질문 기록 {count}건을 {os.path.abspath(path)} 에 저장했습니다.")
    if opts.open:
        os.startfile(os.path.abspath(path))
    return 0


if __name__ == "__main__":
    import clients
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        clients.close_clients()
