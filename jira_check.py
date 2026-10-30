"""Jira 연동 점검 (서버에서 한 번 돌려 본다): python jira_check.py [--email 본인@회사.com]
설정, 인증, 참여자·확인자·스프린트 필드, 활성 스프린트 카드 수를 차례로 확인하고, --email을 주면 그 사람의 카드와 문서 연결을 미리 보여준다.
슬랙 없이 Jira 쪽 문제(주소, 토큰, 권한, 필드 이름)를 먼저 가려내는 용도다. 토큰은 출력하지 않는다."""
import argparse
import sys
from collections import Counter
import asker
import config
import jira_client
import sprint_tasks


def check(email: str = "", out=print, expect: tuple = ()) -> bool:
    """점검 결과를 out으로 한 줄씩 보여주고, 끝까지 문제가 없으면 True.
    expect는 봇이 볼 수 있어야 하는 space 키 목록 (예: CM, ABC). 못 보는 space가 있으면 알려준다"""
    ok = True
    out(f"1. 설정: 주소 {config.JIRA_BASE_URL or '(없음)'} / 이메일 {config.JIRA_EMAIL or '(없음)'} / 토큰 {'있음' if config.JIRA_API_TOKEN else '(없음)'}"
        f" / 호출 방식 {'Cloud ID 주소(범위 지정 토큰·서비스 계정용)' if config.JIRA_CLOUD_ID else '사이트 주소(범위 없는 일반 토큰용)'}")
    if config.JIRA_BASE_URL:
        cloud_id = jira_client.tenant_cloud_id()
        out(f"   사이트 Cloud ID: {cloud_id or '읽지 못함'}" + (" ← 범위 지정 토큰이나 서비스 계정이면 .env의 JIRA_CLOUD_ID에 넣으세요" if cloud_id and not config.JIRA_CLOUD_ID else ""))
    if not jira_client.configured():
        out("   ❌ JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN을 .env에 넣어 주세요.")
        return False
    try:
        me = jira_client.myself()
        out(f"2. 인증: ✅ {me.get('displayName', '?')} 계정으로 접속했어요. (인증 방식: {'Bearer' if jira_client.auth_mode() == 'bearer' else '이메일:토큰'})")
        fields = jira_client.discover_fields(force=True)
        for label, key in ((config.JIRA_FIELD_PARTICIPANT, "participant"), (config.JIRA_FIELD_APPROVER, "approver")):
            found = fields[key]
            out(f"3. 필드 '{label}': " + (f"✅ {len(found)}개 ({', '.join(found)})" if found else "❌ 못 찾음 → 필드 이름이 다르면 JIRA_FIELD_* 환경변수로 바꿔 주세요."))
            ok = ok and bool(found)
        out(f"   필드 'Sprint': {'✅ ' + fields['sprint'] if fields['sprint'] else '⚠️ 못 찾음 (스프린트 이름이 빠집니다)'}")
        cards = sprint_tasks.active_overview()
        by_status = Counter(c["status"] for c in cards)
        out(f"4. 활성 스프린트 카드: {len(cards)}개 (상태별 {dict(by_status.most_common(5))})")
        out(f"   스프린트: {', '.join(sorted({s for c in cards for s in c['sprints']})) or '(없음)'}")
        out(f"   역할 필드가 채워진 카드: 담당 {sum(bool(c['assignee']) for c in cards)} / 확인 {sum(bool(c['approvers']) for c in cards)} / "
            f"참여 {sum(bool(c['participants']) for c in cards)} / 보고 {sum(bool(c['reporter']) for c in cards)}")
        keys = [p["key"] for p in jira_client.visible_projects()]
        out(f"   봇이 볼 수 있는 space {len(keys)}개: {', '.join(keys[:40])}{' …' if len(keys) > 40 else ''}")
        counts = Counter(c["project"] for c in cards)
        out("   활성 스프린트 카드 space별: " + (" · ".join(f"{k} {n}" for k, n in counts.most_common()) or "(없음)"))
        no_sprint = [k for k in keys if k not in counts]
        out(f"   활성 스프린트가 없는 space (완료되지 않은 카드를 모두 보여줘요): {', '.join(no_sprint) or '(없음)'}")
        missing = [k for k in expect if k not in keys]
        if missing:
            out(f"   ❌ 봇이 볼 수 없는 space: {', '.join(missing)} → 그 space에서 봇 계정에 '프로젝트 찾아보기' 권한을 주세요 (그룹이나 프로젝트 역할로 추가).")
            ok = False
        if cards and not any(c["approvers"] or c["participants"] for c in cards):
            out("   ⚠️ 확인자·참여자가 채워진 카드가 하나도 없어요. 필드 이름이나 봇 계정의 space 권한을 확인해 주세요.")
        if email:
            who = asker.Asker("check", lambda: {"email": email, "name": ""})
            account = sprint_tasks.account_for(who)
            out(f"5. {email} → Jira 계정 {account}")
            out(sprint_tasks.format_report(sprint_tasks.build_report(account)))
    except jira_client.JiraError as e:
        out(f"   ❌ {e}")
        return False
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jira 연동 점검")
    parser.add_argument("--email", default="", help="이 사람의 카드와 문서 연결까지 미리 본다")
    parser.add_argument("--expect", default="", help="봇이 볼 수 있어야 하는 space 키 (쉼표로 구분, 예: CM,ABC). 못 보는 space를 알려준다")
    args = parser.parse_args()
    sys.exit(0 if check(args.email, expect=tuple(k.strip().upper() for k in args.expect.split(",") if k.strip())) else 1)
