# Registers SLOTS runners with the org under the pool's label and arms the reaper. Runs at
# every boot of a pool server, from the user data pool.py writes (which exports ORG, POOL,
# NAME, SLOTS, REG_TOKEN, HCLOUD_TOKEN and the removal window).
ETC=${POOL_ETC:-/etc}
RUNNER_ROOT=${POOL_RUNNER_ROOT:-/opt}
# Nothing on a pool server may restart a runner service: a restart is a stop, and a stop cancels
# the job the runner holds ("The runner has received a shutdown signal"). Ubuntu's daily
# unattended-upgrades does exactly that within the first hour after boot: its timer catches up
# on the run the snapshot missed after a random delay of up to an hour, installs each security
# update in its own dpkg call, and needrestart then restarts every service whose process maps an
# old library, the runners and docker among them. So the apt timers are off before the first
# runner registers and an upgrade already under way is stopped (unattended-upgrades finishes the
# package it is on). Security updates still reach the server: they are installed here, once,
# synchronously, before any runner exists to be restarted. The weekly bake (scaler/bake.sh) has
# already installed most of them, so this stays short. A kernel installed here takes effect only
# on a reboot, which a short-lived pool server never does.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer \
  || echo "pool: cannot disable the apt timers" >&2
systemctl stop apt-daily.service apt-daily-upgrade.service \
  || echo "pool: cannot stop a running apt upgrade" >&2
# A failed or timed-out upgrade must not keep the server from registering: without runners it
# is useless and the pool would only create another one.
DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a timeout 600 bash -c \
  'apt-get update -q && apt-get -yq -o Dpkg::Options::=--force-confold upgrade' \
  || echo "pool: boot-time security upgrade failed or timed out" >&2
# From here on needrestart only lists what it would restart, so nothing restarts a runner after
# it registers.
mkdir -p "$ETC/needrestart/conf.d"
cat > "$ETC/needrestart/conf.d/pool.conf" <<'NEEDRESTART'
# Pool server: list services that use old libraries, never restart one (a runner or docker
# restart cancels the job running on it).
$nrconf{restart} = 'l';
NEEDRESTART
date +%s > "$ETC/pool-born"
umask 077
cat > "$ETC/pool.env" <<ENV
HCLOUD_TOKEN='${HCLOUD_TOKEN}'
REMOVE_FROM='${REMOVE_FROM}'
REMOVE_UNTIL='${REMOVE_UNTIL}'
ENV
umask 022
for i in $(seq 1 "$SLOTS"); do
  d=$RUNNER_ROOT/actions-runner-$i
  sudo -u runner bash -c "cd '$d' && ./config.sh --url 'https://github.com/${ORG}' --token '${REG_TOKEN}' \
    --name '${NAME}-${i}' --labels '${POOL},linux,x64' --work _work --unattended --replace"
  (cd "$d" && ./svc.sh install runner && ./svc.sh start)
done
cat > "$ETC/systemd/system/pool-reaper.service" <<UNIT
[Unit]
Description=Delete this pool server when idle at the end of its billing hour
[Service]
Type=oneshot
EnvironmentFile=/etc/pool.env
ExecStart=/usr/local/bin/pool-reaper
UNIT
cat > "$ETC/systemd/system/pool-reaper.timer" <<UNIT
[Unit]
Description=Run pool-reaper every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=5s
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now pool-reaper.timer
