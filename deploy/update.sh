#!/usr/bin/env bash
# GitHub의 최신 코드로 서버를 갱신한다. VM에서 실행:  bash /opt/designbrain/deploy/update.sh
set -euo pipefail
APP_DIR=/opt/designbrain

cd "$APP_DIR"
git pull --ff-only
"$APP_DIR/.venv/bin/pip" install -q -r requirements.txt
# 예전 이름(designbrain-collector)의 사서 서비스는 내리고 지운다
if [ -f /etc/systemd/system/designbrain-collector.service ]; then
  sudo systemctl disable --now designbrain-collector || true
  sudo rm -f /etc/systemd/system/designbrain-collector.service
  sudo systemctl daemon-reload
fi
# 사서 서비스는 나중에 추가됐으므로 없으면 등록한다
if [ ! -f /etc/systemd/system/designbrain-librarian.service ]; then
  sed -e "s|__USER__|$(whoami)|g" -e "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/designbrain-librarian.service" \
    | sudo tee /etc/systemd/system/designbrain-librarian.service > /dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable designbrain-librarian
fi
sudo systemctl restart designbrain designbrain-librarian
echo "갱신 완료: $(git log --oneline -1)"
echo "로그 확인: journalctl -u designbrain -f   (사서: journalctl -u designbrain-librarian -f)"
