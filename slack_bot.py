"""슬랙 Bolt 앱 생성과 이벤트 연결, 소켓 모드 시작/종료"""
import logging
import threading
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import backlog_slack
import config
import proposal_slack
import qa_slack
import slack_service

logger = logging.getLogger(__name__)

slack_app = None
handler = None

def init_slack():
    global slack_app, handler
    
    bot_token = config.SLACK_BOT_TOKEN
    app_token = config.SLACK_APP_TOKEN
    
    if not bot_token or not app_token:
        logger.warning("⚠️ [Slack] 토큰 미설정으로 봇을 시작하지 않습니다.")
        return None

    slack_app = App(token=bot_token)

    # 1. 앱 홈 탭 오픈 이벤트 핸들러 (DM 수신 연결용)
    @slack_app.event("app_home_opened")
    def handle_app_home_opened_events(body, logger):
        logger.info("📱 [Slack] 앱 홈/DM 탭이 열렸습니다.")

    # 2. 채널 내 @봇 멘션 처리
    @slack_app.event("app_mention")
    def handle_app_mention_events(body, client):
        event = body.get("event", {})
        channel_id = event.get("channel")
        say = slack_service.thread_say(client, channel_id)
        thread_ts = slack_service.event_thread_ts(event)
        text = event.get("text", "")

        logger.info("📢 [채널 멘션 수신] %s", text)
        request_text = backlog_slack.backlog_request_text(text)
        if request_text is not None:
            backlog_slack.handle_backlog_mention(client, say, event, request_text)
            return
        slack_service.process_chat_and_reply(client, say, channel_id, thread_ts, event.get("ts"), text, event.get("user"))

    # 3. 1:1 DM 메세지 처리
    @slack_app.event("message")
    def handle_message_events(body, client):
        event = body.get("event", {})
        channel_type = event.get("channel_type")
        
        # 봇 자신의 메세지가 아니고, 순수 DM(im)인 경우만 응답
        if channel_type == "im" and not event.get("bot_id") and not event.get("subtype"):
            user_text = event.get("text", "")
            user_id = event.get("user")
            channel_id = event.get("channel")
            thread_ts = slack_service.event_thread_ts(event)
            say = slack_service.thread_say(client, channel_id)

            logger.info("💬 [DM 수신] 유저(%s): %s", user_id, user_text)
            request_text = backlog_slack.backlog_request_text(user_text)
            if request_text is not None:
                backlog_slack.handle_backlog_mention(client, say, event, request_text)
                return
            slack_service.process_chat_and_reply(client, say, channel_id, thread_ts, event.get("ts"), user_text, user_id)

    # 4. /백로그 커맨드 처리 (슬래시 명령은 스레드 정보가 없어 기획서와 연결하지 않고, 내용 유사도로만 답변에 붙는다)
    @slack_app.command("/백로그")
    def handle_backlog_command(ack, command, say, client):
        ack()
        backlog_slack.register_backlog(client, say, command.get("text", ""), command.get("user_id"), reply_ts=None,
                                       reporter_name=command.get("user_name"))

    # 5. 백로그 리마인더 DM 버튼 (해결됨은 담당자만 처리 가능)
    @slack_app.action("backlog_resolve")
    def handle_backlog_resolve(ack, body, client):
        ack()
        backlog_slack.handle_backlog_button(body, client, resolve=True)

    @slack_app.action("backlog_pending")
    def handle_backlog_pending(ack, body, client):
        ack()
        backlog_slack.handle_backlog_button(body, client, resolve=False)

    # 6. 사서 제안 버튼과 입력창 (담당자만 처리 가능)
    @slack_app.action("proposal_approve")
    def handle_proposal_approve(ack, body, client):
        ack()
        proposal_slack.handle_approve(body, client)

    @slack_app.action("proposal_edit")
    def handle_proposal_edit(ack, body, client):
        ack()
        proposal_slack.handle_edit(body, client)

    @slack_app.action("proposal_hold")
    def handle_proposal_hold(ack, body, client):
        ack()
        proposal_slack.handle_hold(body, client)

    @slack_app.action("proposal_reject")
    def handle_proposal_reject(ack, body, client):
        ack()
        proposal_slack.handle_reject(body, client)

    @slack_app.view("proposal_edit_submit")
    def handle_proposal_edit_submit(ack, body, client):
        ack()
        proposal_slack.handle_edit_submit(body, client)

    @slack_app.view("proposal_reject_submit")
    def handle_proposal_reject_submit(ack, body, client):
        ack()
        proposal_slack.handle_reject_submit(body, client)

    # 7. 답변에 단 👍/👎 리액션을 질문 기록에 반영 (슬랙 앱에 reaction_added/removed 이벤트 구독 필요)
    @slack_app.event("reaction_added")
    def handle_reaction_added(event):
        qa_slack.handle_reaction(event, added=True)

    @slack_app.event("reaction_removed")
    def handle_reaction_removed(event):
        qa_slack.handle_reaction(event, added=False)

    handler = SocketModeHandler(slack_app, app_token)
    return handler

def start_slack_bot():
    h = init_slack()
    if h:
        logger.info("⚡ [Slack] Socket Mode 봇을 시작합니다.")
        h.connect()
        backlog_slack.reminder_stop.clear()
        threading.Thread(target=backlog_slack.reminder_loop, args=(slack_app.client,), daemon=True, name="backlog-reminder").start()
        if config.QA_REPORT_USER_ID:
            qa_slack.report_stop.clear()
            threading.Thread(target=qa_slack.report_loop, args=(slack_app.client,), daemon=True, name="qa-weekly-report").start()
        else:
            logger.info("ℹ️ [질문 기록] QA_REPORT_USER_ID 미설정으로 주간 요약을 보내지 않습니다.")

def stop_slack_bot():
    backlog_slack.reminder_stop.set()
    qa_slack.report_stop.set()
    if handler:
        handler.close()
