#!/usr/bin/env bash
# GCP VM(Ubuntu 24.04)에 DesignBrain을 설치하고 서비스로 등록한다. 여러 번 실행해도 된다.
#   사용법: bash setup.sh <ngrok 고정 도메인>      예) bash setup.sh my-brain.ngrok-free.app
# 홈 폴더(~/.env)에 PC의 .env를 올려 두면 앱 폴더로 옮긴다. ngrok 인증 토큰이 없으면 실행 중에 묻는다.
set -euo pipefail

NGROK_DOMAIN=${1:?"ngrok 고정 도메인을 넣어 주세요. 예) bash setup.sh my-brain.ngrok-free.app"}
REPO=https://github.com/JeongMoongi/DesignBrain.git
APP_DIR=/opt/designbrain
APP_USER=$(whoami)

echo "== 1. 스왑 1GB (e2-micro는 메모리가 1GB라 패키지 설치 중 부족하지 않도록)"
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
fi

echo "== 2. 패키지 (Python, git, ngrok)"
sudo apt-get update -q
sudo apt-get install -y -q python3-venv git curl
if ! command -v ngrok > /dev/null; then
  curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc > /dev/null
  echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list > /dev/null
  sudo apt-get update -q
  sudo apt-get install -y -q ngrok
fi
if ! grep -qs "authtoken" "$HOME/.config/ngrok/ngrok.yml"; then
  # 명령줄 인자로 받지 않는다 (셸 기록에 토큰이 남지 않도록)
  read -rsp "ngrok 인증 토큰(ngrok 대시보드 > Your Authtoken)을 붙여넣고 Enter: " NGROK_TOKEN
  echo
  ngrok config add-authtoken "$NGROK_TOKEN" > /dev/null
fi

echo "== 3. 코드와 파이썬 패키지"
sudo mkdir -p "$APP_DIR"
sudo chown "$APP_USER" "$APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO" "$APP_DIR"
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ] && [ -f "$HOME/.env" ]; then
  mv "$HOME/.env" "$APP_DIR/.env"
fi
if [ ! -f "$APP_DIR/.env" ]; then
  echo "!! .env 가 없습니다. PC의 .env를 VM 홈 폴더에 올린 뒤 다시 실행해 주세요 (deploy/README.md 4단계)."
  exit 1
fi
chmod 600 "$APP_DIR/.env"

echo "== 4. 서비스 등록 (부팅 시 자동 시작, 죽으면 다시 시작)"
for name in designbrain designbrain-librarian ngrok; do
  sed -e "s|__USER__|$APP_USER|g" -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__NGROK_DOMAIN__|$NGROK_DOMAIN|g" \
      -e "s|__NGROK_BIN__|$(command -v ngrok)|g" \
    "$APP_DIR/deploy/$name.service" | sudo tee "/etc/systemd/system/$name.service" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now designbrain designbrain-librarian ngrok
sudo systemctl restart designbrain designbrain-librarian ngrok

echo "== 완료. 로그 확인: journalctl -u designbrain -f"
