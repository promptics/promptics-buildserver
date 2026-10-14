#!/bin/bash
# Deletes this server when it is idle in the last minutes of its billing hour (pool.py's rule).
# Billing counts from creation, a minute or two before boot, so /etc/pool-born plus BOOT_LAG
# approximates the server's own clock; the controller's sweep uses the exact `created`.
set -u
BOOT_LAG=2
born=$(cat /etc/pool-born)
minute=$(( ( ( $(date +%s) - born ) / 60 + BOOT_LAG ) % 60 ))
[ "$minute" -ge "$REMOVE_FROM" ] && [ "$minute" -lt "$REMOVE_UNTIL" ] || exit 0
# A job runs while a Runner.Worker process exists; an idle runner would already have taken a
# queued job, so idle here means nothing waits for this server either.
pgrep -f Runner.Worker >/dev/null && exit 0
for d in /opt/actions-runner-*; do [ -f "$d/.runner" ] && (cd "$d" && ./svc.sh stop >/dev/null); done
if pgrep -f Runner.Worker >/dev/null; then          # a job landed between the check and the stop
  for d in /opt/actions-runner-*; do [ -f "$d/.runner" ] && (cd "$d" && ./svc.sh start >/dev/null); done
  exit 0
fi
id=$(curl -fsS http://169.254.169.254/hetzner/v1/metadata/instance-id)
logger -t pool-reaper "idle at minute $minute of the billing hour: deleting server $id"
curl -fsS -X DELETE -H "Authorization: Bearer ${HCLOUD_TOKEN}" "https://api.hetzner.cloud/v1/servers/${id}"
