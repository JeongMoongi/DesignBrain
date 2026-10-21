"""문서 진단 결과를 모아 패턴별로 보여준다. (진단은 문서를 동기화할 때마다 남는다 → doc_diagnosis.py)

    python diagnosis_report.py            # 진단 종류별 문서 수와 해당 문서 목록
    python diagnosis_report.py --out 진단.md

PC에서 실행해도 같은 Qdrant Cloud를 읽는다. 전체 문서의 진단을 새로 받으려면 resync_all.py를 먼저 실행한다.
"""
import argparse
import os
import sys
import config
import doc_diagnosis


def load():
    import clients
    rows, offset = [], None
    while True:
        points, offset = clients.get_qdrant().scroll(collection_name=config.DIAGNOSIS_COLLECTION, limit=256, offset=offset)
        rows += [p.payload for p in points]
        if offset is None:
            return rows


def build_report(rows) -> str:
    flagged = [r for r in rows if r.get("findings")]
    lines = [f"# 문서 진단 패턴", "", f"진단한 문서 {len(rows)}개 중 {len(flagged)}개에서 발견", ""]
    for kind, label in doc_diagnosis.KINDS.items():
        hits = [(r, f) for r in rows for f in r.get("findings", []) if f["kind"] == kind]
        if not hits:
            continue
        lines += [f"## {label} — 문서 {len(hits)}개, 총 {sum(f['count'] for _, f in hits)}건", ""]
        for r, f in sorted(hits, key=lambda h: -h[1]["count"]):
            where = f" ({r['path']})" if r.get("path") else ""
            lines.append(f"- [{r['title']}]({r['url']}){where}: {', '.join(f['details'][:doc_diagnosis.DETAIL_SHOWN])}"
                         + (f" 외 {len(f['details']) - doc_diagnosis.DETAIL_SHOWN}개" if len(f["details"]) > doc_diagnosis.DETAIL_SHOWN else ""))
        lines.append("")
    return "\n".join(lines)


def main(args):
    parser = argparse.ArgumentParser(description="문서 진단 패턴 보고서")
    parser.add_argument("--out", help="마크다운 파일로 저장")
    opts = parser.parse_args(args)
    report = build_report(load())
    if opts.out:
        with open(opts.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"저장: {os.path.abspath(opts.out)}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)은 '—' 등을 출력하지 못한다
        print(report)
    return 0


if __name__ == "__main__":
    import clients
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        clients.close_clients()
