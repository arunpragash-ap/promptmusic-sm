#!/bin/bash
# EC2 user data (Amazon Linux 2023, arm64 or x86_64): PromptMusic Studio behind Caddy HTTPS.
#   * no AWS keys: boto3 uses the instance role (app/iam-policy.json + AmazonSSMManagedInstanceCore)
#   * no SSH: administer with Session Manager
#   * HTTPS without a domain: https://<public-ip-with-dashes>.sslip.io (Let's Encrypt via Caddy)
# Log of this script on the instance: /var/log/cloud-init-output.log
set -euxo pipefail
REPO_RAW=https://raw.githubusercontent.com/arunpragash-ap/promptmusic-sm/main
ARCH=$(uname -m); case $ARCH in aarch64) GOARCH=arm64;; x86_64) GOARCH=amd64;; *) echo "unsupported $ARCH"; exit 1;; esac

dnf install -y python3.11 python3.11-pip

# --- the app -------------------------------------------------------------------
id studio >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/studio studio
curl -fsSL "$REPO_RAW/app/requirements.txt" -o /opt/studio/requirements.txt
curl -fsSL "$REPO_RAW/app/studio.py"        -o /opt/studio/studio.py
python3.11 -m venv /opt/studio/venv
/opt/studio/venv/bin/pip install --quiet --upgrade pip
/opt/studio/venv/bin/pip install --quiet -r /opt/studio/requirements.txt
mkdir -p /opt/studio/outputs
chown -R studio:studio /opt/studio

if [ ! -f /etc/promptmusic-studio.env ]; then
  install -m 600 -o root -g root /dev/null /etc/promptmusic-studio.env
  cat > /etc/promptmusic-studio.env <<'ENV'
AWS_REGION=ap-south-1
PM_HOST=127.0.0.1
PORT=7860
PM_REQUIRE_LOGIN=1
PM_OUTPUT_DIR=/opt/studio/outputs
# Set logins, then: sudo systemctl restart promptmusic-studio
PM_USERS=
ENV
fi

cat > /etc/systemd/system/promptmusic-studio.service <<'UNIT'
[Unit]
Description=PromptMusic Studio (Gradio GUI for the SageMaker endpoint)
After=network-online.target
Wants=network-online.target

[Service]
User=studio
EnvironmentFile=/etc/promptmusic-studio.env
ExecStart=/opt/studio/venv/bin/python /opt/studio/studio.py
Restart=always
RestartSec=15
NoNewPrivileges=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT

# --- HTTPS reverse proxy (Caddy) ----------------------------------------------------
curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=$GOARCH" -o /usr/local/bin/caddy
chmod 755 /usr/local/bin/caddy
id caddy >/dev/null 2>&1 || useradd --system --home-dir /var/lib/caddy --shell /sbin/nologin caddy

cat > /usr/local/bin/studio-https-config <<'SH'
#!/bin/bash
# Writes /etc/caddy/Caddyfile for the instance's CURRENT public IP (re-run after attaching an Elastic IP).
# Own domain instead of sslip.io: echo studio.example.com | sudo tee /etc/caddy/domain
set -euo pipefail
T=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
IP=$(curl -fsS -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/public-ipv4)
HOST=$(cat /etc/caddy/domain 2>/dev/null || echo "${IP//./-}.sslip.io")
mkdir -p /etc/caddy
printf '%s {\n\treverse_proxy 127.0.0.1:7860\n}\n' "$HOST" > /etc/caddy/Caddyfile
echo "https://$HOST" > /etc/caddy/url.txt
echo "Studio URL: https://$HOST"
SH
chmod 755 /usr/local/bin/studio-https-config

cat > /etc/systemd/system/caddy.service <<'UNIT'
[Unit]
Description=Caddy HTTPS reverse proxy for PromptMusic Studio
After=network-online.target
Wants=network-online.target

[Service]
User=caddy
StateDirectory=caddy
Environment=XDG_DATA_HOME=/var/lib/caddy XDG_CONFIG_HOME=/var/lib/caddy
ExecStartPre=+/usr/local/bin/studio-https-config
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=always
RestartSec=15
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now promptmusic-studio caddy
echo "DONE. URL: $(cat /etc/caddy/url.txt). Next: set PM_USERS in /etc/promptmusic-studio.env"
