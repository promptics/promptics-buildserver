# Registers SLOTS runners with the org under the pool's label and arms the reaper. Runs at
# every boot of a pool server, from the user data pool.py writes (which exports ORG, POOL,
# NAME, SLOTS, REG_TOKEN, HCLOUD_TOKEN and the removal window).
date +%s > /etc/pool-born
umask 077
cat > /etc/pool.env <<ENV
HCLOUD_TOKEN='${HCLOUD_TOKEN}'
REMOVE_FROM='${REMOVE_FROM}'
REMOVE_UNTIL='${REMOVE_UNTIL}'
ENV
umask 022
for i in $(seq 1 "$SLOTS"); do
  d=/opt/actions-runner-$i
  sudo -u runner bash -c "cd '$d' && ./config.sh --url 'https://github.com/${ORG}' --token '${REG_TOKEN}' \
    --name '${NAME}-${i}' --labels '${POOL},linux,x64' --work _work --unattended --replace"
  (cd "$d" && ./svc.sh install runner && ./svc.sh start)
done
cat > /etc/systemd/system/pool-reaper.service <<UNIT
[Unit]
Description=Delete this pool server when idle at the end of its billing hour
[Service]
Type=oneshot
EnvironmentFile=/etc/pool.env
ExecStart=/usr/local/bin/pool-reaper
UNIT
cat > /etc/systemd/system/pool-reaper.timer <<UNIT
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
