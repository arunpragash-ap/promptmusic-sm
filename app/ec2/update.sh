#!/bin/bash
# Run on the instance (Session Manager) to pull the latest studio.py from GitHub and restart.
set -euo pipefail
REPO_RAW=https://raw.githubusercontent.com/arunpragash-ap/promptmusic-sm/main
sudo curl -fsSL "$REPO_RAW/app/studio.py" -o /opt/studio/studio.py
sudo curl -fsSL "$REPO_RAW/app/requirements.txt" -o /opt/studio/requirements.txt
sudo /opt/studio/venv/bin/pip install --quiet -r /opt/studio/requirements.txt
sudo chown -R studio:studio /opt/studio
sudo systemctl restart promptmusic-studio
sudo systemctl --no-pager --lines=5 status promptmusic-studio
