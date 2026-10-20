"""답변 품질 평가 (에이전트 전환용). eval/agent_cases.json의 질문마다 답변을 만들고, 채점 모델이 expect(들어가야 할 내용)와
avoid(틀린 답의 예) 기준으로 채점한다. 답변 전문은 마크다운 파일로 남겨 사람이 직접 비교할 수 있게 한다.

    python eval_agent.py                                  # 현재 설정(config.ANSWER_MODE 등)으로
    python eval_agent.py --mode classic                   # 기존 방식 (검색 1회)
    python eval_agent.py --mode agent --model gpt-6.1-sol --effort low
    python eval_agent.py multi- consult-02                # id가 이 값들로 시작하는 질문만

실제 Qdrant/OpenAI를 사용한다 (채점 포함 질문마다 여러 번 호출).
"""
import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import ai_engine
import clients
import config
from eval_search import chat_history

CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval", "agent_cases.json")
JUDGE_MODEL = "gpt-6.1-sol"
JUDGE_PROMPT = (
    "너는 게임 기획 Q&A 봇의 답변 채점자다. [질문], [채점 기준], [답변]을 보고 JSON으로만 답하라.\n"
    "- expect: 기준의 expect 항목마다 답변이 그 내용을 담고 있으면 true. 표현이 달라도 의미가 같으면 true.\n"
    "- avoid: 기준의 avoid 항목마다 답변이 그 잘못을 저질렀으면 true.\n"
    "- comment: 답변의 가장 큰 장점이나 문제를 한국어 한 문장으로.\n"
    '형식: {"expect": [true, false, ...], "avoid": [false, ...], "comment": "..."}'
)


def load_cases(prefixes):
    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    return [c for c in cases if not prefixes or c["id"].startswith(tuple(prefixes))]


def judge(case, answer):
    criteria = {"expect": case["expect"], "avoid": case.get("avoid", []), "note": case.get("note", "")}
    response = clients.get_openai().responses.create(
        model=JUDGE_MODEL, instructions=JUDGE_PROMPT, reasoning={"effort": "low"},
        text={"format": {"type": "json_object"}},
        input=f"아래 답변을 채점해 json으로 답하라.\n\n[질문]\n{case['question']}\n\n[채점 기준]\n{json.dumps(criteria, ensure_ascii=False)}\n\n[답변]\n{answer}")
    return json.loads(response.output_text)


def run_case(case):
    started = time.time()
    res = ai_engine.ask_ai_with_history(case["question"], chat_history(case))
    elapsed = time.time() - started
    if res.get("status") != "success":
        answer = f"(실패: {res.get('stage')} - {res.get('reason')})"
    else:
        answer = res["AI_답변"]
    verdict = judge(case, answer)
    return {"case": case, "answer": answer, "seconds": elapsed, "usage": res.get("usage", {}),
            "tools": res.get("search_query", ""), "verdict": verdict}


def main(args):
    parser = argparse.ArgumentParser(description="답변 품질 평가")
    parser.add_argument("prefixes", nargs="*", help="이 값들로 시작하는 id만")
    parser.add_argument("--mode", choices=["classic", "agent"], default=config.ANSWER_MODE)
    parser.add_argument("--model", default=config.AGENT_MODEL, help="에이전트 모델")
    parser.add_argument("--effort", default=config.AGENT_REASONING_EFFORT, help="에이전트 추론 강도")
    parser.add_argument("--out", help="답변 전문을 저장할 마크다운 파일")
    opts = parser.parse_args(args)
    config.ANSWER_MODE, config.AGENT_MODEL, config.AGENT_REASONING_EFFORT = opts.mode, opts.model, opts.effort
    label = "classic" if opts.mode == "classic" else f"{opts.model} ({opts.effort})"

    cases = load_cases(opts.prefixes)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run_case, cases))

    total_expect = total_hit = total_avoid = 0
    lines = [f"# 답변 평가: {label} ({datetime.now():%Y-%m-%d %H:%M})", ""]
    print(f"\n=== {label} ===")
    for r in results:
        case, v = r["case"], r["verdict"]
        hit, n = sum(map(bool, v.get("expect", []))), len(case["expect"])
        bad = sum(map(bool, v.get("avoid", [])))
        total_expect, total_hit, total_avoid = total_expect + n, total_hit + hit, total_avoid + bad
        usage = r["usage"]
        cost = f" | 도구 {usage.get('tool_calls', 0)}회, 토큰 {usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}" if usage else ""
        summary = f"{case['id']:<11} 기대 {hit}/{n}  오답 {bad}  {r['seconds']:.0f}s{cost}"
        print(f"{summary}\n   {v.get('comment', '')}")
        lines += [f"## {case['id']} — {case['question']}", "", f"`{summary}`", "", f"> 채점: {v.get('comment', '')}", "",
                  f"도구: {r['tools']}", "", r["answer"], ""]
    print(f"\n합계: 기대 내용 {total_hit}/{total_expect}, 오답 {total_avoid}건")

    out = opts.out or f"eval_result_{opts.mode}_{datetime.now():%m%d_%H%M}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines + [f"**합계: 기대 내용 {total_hit}/{total_expect}, 오답 {total_avoid}건**"]))
    print(f"답변 전문: {os.path.abspath(out)}")
    return 0


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.WARNING)
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        clients.close_clients()
