#!/bin/bash
# First-boot setup of an Oracle Cloud Always Free VM (Ubuntu 24.04, ARM) for OrderFlow Terminal:
# Node 22, the app as a systemd service, Caddy with automatic HTTPS on <ip>.sslip.io, and a timer that pulls
# the branch every 3 minutes and redeploys on new commits (like Render's auto-deploy).
# The secrets block below (the placeholder line inside the oft.env heredoc) is filled in at launch time;
# this template holds no secrets.
set -euxo pipefail
exec > >(tee -a /var/log/oft-setup.log) 2>&1
export DEBIAN_FRONTEND=noninteractive
BRANCH=eliot-top-1
REPO=https://github.com/hobbit7771/elliott-wave-analyzer

apt-get update
apt-get install -y ca-certificates curl git gnupg build-essential python3 postgresql-client debian-keyring debian-archive-keyring apt-transport-https
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy

# Oracle's Ubuntu images end the INPUT chain with "REJECT --reject-with icmp-host-prohibited": the allow rules
# must go BEFORE it (its position differs between image versions, so look it up instead of assuming a number)
REJ=$(iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')
iptables -I INPUT "${REJ:-1}" -m state --state NEW -p tcp --dport 443 -j ACCEPT
iptables -I INPUT "${REJ:-1}" -m state --state NEW -p tcp --dport 80 -j ACCEPT
netfilter-persistent save || true
iptables -L INPUT --line-numbers -n

# The Oracle Cloud Agent's Run Command plugin runs as "ocarun": let it administer the VM (owner's decision; there is
# no SSH access from the maintainer's environment)
echo 'ocarun ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/101-oracle-cloud-agent-run-command
chmod 440 /etc/sudoers.d/101-oracle-cloud-agent-run-command
visudo -cf /etc/sudoers.d/101-oracle-cloud-agent-run-command

id oft >/dev/null 2>&1 || useradd -r -m -d /opt/oft -s /bin/bash oft
mkdir -p /etc/oft /var/lib/oft /var/www/oft
chown oft:oft /var/lib/oft
cat > /etc/oft/oft.env <<'ENVEOF'
__OFT_ENV__
ENVEOF
chmod 600 /etc/oft/oft.env

[ -d /opt/oft/repo ] || sudo -u oft git clone -b "$BRANCH" --depth 50 "$REPO" /opt/oft/repo

cat > /usr/local/bin/oft-update <<UPDEOF
#!/bin/bash
# Pull the branch; on a new commit: install, build, restart. Writes a public status file (no secrets).
set -uo pipefail
exec >> /var/lib/oft/update.log 2>&1
echo "== \$(date -u +%FT%TZ) oft-update \${1:-}"
cd /opt/oft/repo
sudo -u oft git fetch -q origin $BRANCH || { echo "git fetch failed"; exit 0; }
NEW=\$(sudo -u oft git rev-parse origin/$BRANCH); CUR=\$(cat /var/lib/oft/deployed 2>/dev/null || echo none)
[ "\$NEW" = "\$CUR" ] && [ "\${1:-}" != force ] && exit 0
sudo -u oft git reset -q --hard "\$NEW"
cd orderflow-terminal
if sudo -u oft bash -c 'npm ci --include=dev --no-audit --no-fund && npm run build' > /var/lib/oft/build.log 2>&1; then
  echo "\$NEW" > /var/lib/oft/deployed; systemctl restart oft; R=ok
else R=build-failed; fi
echo "commit \$NEW: \$R"
printf '{"commit":"%s","result":"%s","at":"%s"}\n' "\$NEW" "\$R" "\$(date -u +%FT%TZ)" > /var/www/oft/deploy.json
UPDEOF
chmod 755 /usr/local/bin/oft-update

cat > /etc/systemd/system/oft.service <<'SVCEOF'
[Unit]
Description=OrderFlow Terminal
After=network-online.target
Wants=network-online.target
[Service]
User=oft
WorkingDirectory=/opt/oft/repo/orderflow-terminal
EnvironmentFile=/etc/oft/oft.env
ExecStart=/usr/bin/node --disable-warning=ExperimentalWarning dist/server/index.js
Restart=always
RestartSec=5
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
SVCEOF
cat > /etc/systemd/system/oft-update.service <<'U1'
[Unit]
Description=OrderFlow Terminal auto-deploy
[Service]
Type=oneshot
ExecStart=/usr/local/bin/oft-update
U1
cat > /etc/systemd/system/oft-update.timer <<'U2'
[Unit]
Description=Pull and redeploy OrderFlow Terminal every 3 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
[Install]
WantedBy=timers.target
U2
# last 300 log lines for the owner (basic auth with the owner token), refreshed every minute
cat > /usr/local/bin/oft-logs <<'L1'
#!/bin/bash
# 1) last 300 app log lines for the owner (served at /__logs behind basic auth)
journalctl -u oft -n 300 --no-pager > /var/www/oft/logs.txt.tmp 2>&1; mv /var/www/oft/logs.txt.tmp /var/www/oft/logs.txt
# 2) machine status report into Supabase (oft.settings 'oracle:status'): readable by the maintainer even when the VM
#    is unreachable from outside. Contains no secrets (the env file is never read into the report).
python3 - <<'PY' > /var/lib/oft/status.json
import json, subprocess, datetime
def sh(c):
    try: return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=30).stdout[-6000:]
    except Exception as e: return str(e)
print(json.dumps({
  "at": datetime.datetime.utcnow().isoformat() + "Z",
  "services": sh("systemctl is-active oft caddy oft-update.timer oft-logs.timer oracle-cloud-agent snap.oracle-cloud-agent.oracle-cloud-agent 2>&1"),
  "listening": sh("ss -ltn"),
  "iptables": sh("iptables -L INPUT --line-numbers -n"),
  "deploy": sh("cat /var/www/oft/deploy.json"),
  "update_log": sh("tail -40 /var/lib/oft/update.log"),
  "build_log": sh("tail -60 /var/lib/oft/build.log"),
  "setup_log": sh("grep -aiE 'error|fail|denied|OFT setup' /var/log/oft-setup.log | tail -40"),
  "caddy": sh("journalctl -u caddy -n 30 --no-pager"),
  "app": sh("journalctl -u oft -n 60 --no-pager"),
  "health": sh("curl -s -m 5 http://127.0.0.1:8080/api/health"),
  "mem": sh("free -m"), "disk": sh("df -h /"),
}))
PY
val() { grep "^$1=" /etc/oft/oft.env | head -1 | cut -d= -f2-; }
REF=$(val SUPABASE_PROJECT_REF); REG=$(val SUPABASE_REGION); USR=$(val OFT_DB_USER)
# psql substitutes :'var' only in commands read from stdin (not in -c)
echo "insert into oft.settings(k, v) values ('oracle:status', :'status'::jsonb) on conflict (k) do update set v = excluded.v, updated_at = now();" |
  PGPASSWORD="$(val OFT_DB_PASSWORD)" psql -q -v ON_ERROR_STOP=1 -v status="$(cat /var/lib/oft/status.json)" \
    "host=aws-0-${REG:-eu-central-1}.pooler.supabase.com port=5432 dbname=postgres user=${USR:-oft_app}.${REF} sslmode=require" \
  > /var/lib/oft/report.log 2>&1 || true
L1
chmod 755 /usr/local/bin/oft-logs
cat > /etc/systemd/system/oft-logs.service <<'L2'
[Service]
Type=oneshot
ExecStart=/usr/local/bin/oft-logs
L2
cat > /etc/systemd/system/oft-logs.timer <<'L3'
[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
L3

IP=$(curl -fsS https://api.ipify.org || curl -fsS https://ifconfig.me)
HOST="${IP//./-}.sslip.io"
set +x # never trace secrets into the setup log / serial console
HASH=$(caddy hash-password --plaintext "$(grep '^OWNER_TOKEN=' /etc/oft/oft.env | cut -d= -f2-)")
set -x
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
	encode zstd gzip
	handle /__deploy {
		root * /var/www/oft
		rewrite * /deploy.json
		file_server
	}
	handle /__logs {
		basic_auth {
			owner $HASH
		}
		root * /var/www/oft
		rewrite * /logs.txt
		file_server
	}
	handle {
		reverse_proxy localhost:8080
	}
}
CADDY
chown -R caddy:caddy /var/www/oft || true
chmod 755 /var/www/oft

systemctl daemon-reload
systemctl enable oft oft-update.timer oft-logs.timer
/usr/local/bin/oft-update force
systemctl start oft-update.timer oft-logs.timer
systemctl restart caddy
/usr/local/bin/oft-logs || true
echo "OFT setup done: https://$HOST"
