# GCP 무료 VM 배포

서버(FastAPI + 슬랙 소켓 모드 + 백로그 리마인더)를 GCP Always Free e2-micro VM에서 상시 실행한다.
노션 웹훅은 VM에서 도는 ngrok 고정 도메인으로 받는다. Qdrant는 기존 Qdrant Cloud를 그대로 쓴다.

- 인스턴스는 **반드시 1개**만 띄운다 (2개면 백로그 리마인더 DM이 두 번 간다).
- 서버 메모리 사용량은 약 140MB라 e2-micro(1GB)로 충분하다.

## 1. 비용 안전장치 (먼저)

GCP 콘솔 > 결제 > 예산 및 알림 > 예산 만들기: 금액 1달러, 알림 50/90/100%.
무료 범위를 벗어나는 설정(디스크 종류, 리전 등)을 실수해도 메일로 바로 알 수 있다.

## 2. VM 만들기

Compute Engine > VM 인스턴스 > 인스턴스 만들기

| 항목 | 값 | 이유 |
|---|---|---|
| 리전 | `us-west1`(오리건), `us-central1`, `us-east1` 중 하나 | 무료 VM은 이 세 리전만 |
| 머신 유형 | `e2-micro` | 무료 VM |
| 부팅 디스크 | Ubuntu 24.04 LTS (x86/64), **표준 영구 디스크**, 30GB 이하 | 기본값(균형 있는 영구 디스크)은 무료가 아니다. Python 3.12 기본 포함 |
| 방화벽 | HTTP/HTTPS 허용 체크하지 않음 | ngrok이 바깥으로 연결하므로 들어오는 포트가 필요 없다 |

## 3. ngrok 고정 도메인과 인증 토큰 확인

[ngrok 대시보드](https://dashboard.ngrok.com)에서

- **Domains**: 무료 계정은 고정 도메인 1개를 받을 수 있다 (`xxx.ngrok-free.app`). 없으면 만든다.
- **Your Authtoken**: 설치 중에 붙여넣는다.

노션 자동화의 웹훅 주소가 이 고정 도메인(`https://<도메인>/webhook/notion`)과 같으면 노션 설정은 바꿀 필요가 없다.
다르면 6단계에서 노션 자동화의 주소를 고친다.

## 4. 설치

1. VM 목록에서 **SSH** 버튼을 눌러 브라우저 터미널을 연다.
2. 터미널 오른쪽 위 **파일 업로드**로 PC의 `.env`를 올린다 (홈 폴더에 저장된다).
3. 아래를 실행한다. 중간에 ngrok 인증 토큰을 묻는다.

```bash
curl -fsSL https://raw.githubusercontent.com/JeongMoongi/DesignBrain/main/deploy/setup.sh -o setup.sh
bash setup.sh <ngrok 고정 도메인>
```

## 5. 전환

PC와 VM이 동시에 돌면 같은 ngrok 도메인을 두고 다투고 리마인더가 두 번 갈 수 있으므로, **PC 서버와 PC의 ngrok을 먼저 끄고** 4단계를 실행한다.

확인:

- `journalctl -u designbrain -f` 에 `⚡ [Slack] Socket Mode 봇을 시작합니다.` 가 보인다.
- 슬랙에서 질문하면 답변이 온다.
- 노션에서 '편집 완료 후 안내' 버튼을 누르면 로그에 `🎉 [동기화 완료]` 가 찍힌다.

## 6. 노션 웹훅 주소 (3단계에서 주소가 달랐다면)

노션 자동화 설정에서 웹훅 주소를 `https://<ngrok 고정 도메인>/webhook/notion` (삭제는 `/webhook/notion/delete`)으로 바꾼다.
`X-Webhook-Secret` 헤더는 그대로 둔다.

## 운영

| 할 일 | 명령 (VM에서) |
|---|---|
| 로그 보기 | `journalctl -u designbrain -f` |
| 최신 코드로 갱신 | `bash /opt/designbrain/deploy/update.sh` |
| 재시작 | `sudo systemctl restart designbrain` |
| ngrok 상태 | `systemctl status ngrok` |
| 전체 재동기화 | `cd /opt/designbrain && .venv/bin/python resync_all.py` |
| 링크 하위 등록 | `cd /opt/designbrain && .venv/bin/python import_notion.py <링크>` |

서버와 ngrok은 VM이 재부팅되어도 자동으로 시작되고, 죽으면 5초 뒤 다시 시작된다.
`.env`를 바꾼 뒤에는 재시작해야 반영된다.

### 답변 방식 (`.env`)

| 설정 | 기본값 | 설명 |
|---|---|---|
| `ANSWER_MODE` | `classic` | `agent`로 바꾸면 모델이 기획서를 직접 찾아가며 답한다 (agent.py) |
| `AGENT_MODEL` | `gpt-6-luna` | 에이전트 모델 (예: `gpt-6.1-sol`) |
| `AGENT_REASONING_EFFORT` | `medium` | 추론 강도 (`low` / `medium` / `high`) |

바꾸기 전에 PC에서 `python eval_agent.py --mode agent --model <모델>`로 평가해 비교할 수 있다.
