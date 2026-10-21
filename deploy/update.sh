#!/usr/bin/env bash
# GitHub의 최신 코드로 서버를 갱신한다. VM에서 실행:  bash /opt/designbrain/deploy/update.sh
set -euo pipefail
APP_DIR=/opt/designbrain

cd "$APP_DIR"
git pull --ff-only
"$APP_DIR/.venv/bin/pip" install -q -r requirements.txt
# 수집가 서비스는 나중에 추가됐으므로 없으면 등록한다
if [ ! -f /etc/systemd/system/designbrain-collector.service ]; then
  sed -e "s|__USER__|$(whoami)|g" -e "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/designbrain-collector.service"     | sudo tee /etc/systemd/system/designbrain-collector.service > /dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable designbrain-collector
fi
sudo systemctl restart designbrain designbrain-collector
echo "갱신 완료: $(git log --oneline -1)"
echo "로그 확인: journalctl -u designbrain -f   (수집가: journalctl -u designbrain-collector -f)"
