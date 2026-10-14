"""검색/답변 품질 평가. eval/search_cases.json의 질문마다 기대한 기획서(docs)와 DB 항목(items)을 찾는지 확인한다.

    python eval_search.py             # 검색 모드: 답변에 쓰일 검색 결과에 기대 문서/항목이 있는지 (빠르고 저렴)
    python eval_search.py --answer    # 답변 모드: 실제 답변의 참고 문서 링크와 본문까지 확인 (질문마다 GPT 호출)
    python eval_search.py name- follow-03   # id가 이 값들로 시작하는 질문만

실제 Qdrant/OpenAI를 사용한다. 검색 방식을 바꾼 뒤 이전과 결과를 비교할 때 쓴다.
"""
import json
import logging
import os
import re
import sys
from collections import defaultdict
import ai_engine

CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval", "search_cases.json")
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(https?://[^)]+\)")


def load_cases(prefixes):
    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    return [c for c in cases if not prefixes or c["id"].startswith(tuple(prefixes))]


def chat_history(case):
    """history: [질문, 답변, 질문, 답변, ...]"""
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": text}
            for i, text in enumerate(case.get("history", []))]


def check_search(case):
    """답변 생성 직전까지(재작성 → 검색)만 실행해 GPT에 넘어갈 검색 결과를 확인한다"""
    search_query = ai_engine.rewrite_search_query(case["question"], chat_history(case))
    results, _ = ai_engine.search_chunks(search_query, ai_engine.get_embedding(search_query))
    titles = list(dict.fromkeys(hit.payload.get("title") for hit in results))
    items = {hit.payload.get("item") for hit in results}

    problems = [f"문서 없음: {d}" for d in case["docs"] if d not in titles]
    problems += [f"항목 없음: {i}" for i in case.get("items", []) if i not in items]
    detail = f"문서 순위: {', '.join(f'{d} {titles.index(d) + 1}위' for d in case['docs'] if d in titles) or '-'}"
    if search_query != case["question"]:
        detail += f" | 재작성: {search_query}"
    return problems, detail


def check_answer(case):
    """실제 답변의 참고 문서 링크가 기대 문서를 모두 포함하는지, 기대 항목이 답변에 언급되는지 확인한다"""
    res = ai_engine.ask_ai_with_history(case["question"], chat_history(case))
    if res.get("status") != "success":
        return [f"답변 실패: {res.get('stage')} - {res.get('reason')}"], "", 0
    answer = res["AI_답변"]
    body, _, sources = answer.rpartition("- 참고 문서 :")
    if not body:
        body, sources = answer, ""
    cited = LINK_PATTERN.findall(sources)
    extra = [c for c in cited if c not in case["docs"]]

    problems = [f"참고 문서에 없음: {d}" for d in case["docs"] if d not in cited]
    if not case["docs"] and cited:
        problems.append("범위 밖 질문인데 참고 문서가 붙음")
    normalized_body = ai_engine.normalize_name(body)
    problems += [f"답변에 언급 없음: {i}" for i in case.get("items", [])
                 if not any(alias in normalized_body for alias in ai_engine.name_aliases(i))]
    detail = f"참고 문서: {', '.join(cited) or '없음'}"
    return problems, detail, len(extra)


def main(args):
    logging.disable(logging.WARNING)
    answer_mode = "--answer" in args
    cases = load_cases([a for a in args if not a.startswith("--")])
    if not cases:
        print("해당하는 질문이 없습니다.")
        return 1

    passed = defaultdict(int)
    total = defaultdict(int)
    extra_links = cited_cases = 0
    for case in cases:
        if not answer_mode and not case["docs"]:
            continue  # 범위 밖 질문은 검색 결과가 항상 있으므로 답변 모드에서만 본다
        try:
            if answer_mode:
                problems, detail, extra = check_answer(case)
                extra_links += extra
                cited_cases += 1
            else:
                problems, detail = check_search(case)
        except Exception as e:
            problems, detail = [f"실행 오류: {type(e).__name__}: {e}"], ""
        total[case["category"]] += 1
        passed[case["category"]] += not problems
        print(f"{'✅' if not problems else '❌'} [{case['id']}] {case['question']}")
        if problems:
            print(f"     {' / '.join(problems)}")
        if detail:
            print(f"     {detail}")
        if case.get("memo"):
            print(f"     메모: {case['memo']}")

    print("\n" + ("[답변 모드]" if answer_mode else "[검색 모드]"))
    for category in total:
        print(f"  {category}: {passed[category]}/{total[category]}")
    print(f"  전체: {sum(passed.values())}/{sum(total.values())}")
    if answer_mode:
        print(f"  기대 외 참고 문서 링크: {extra_links}개 (질문 {cited_cases}개)")
    ai_engine.close_clients()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
