#!/bin/bash
# Deletes this server when it is idle in the last minutes of its billing hour (pool.py's rule).
# Billing counts from creation, a minute or two before boot, so /etc/pool-born plus BOOT_LAG
# approximates the server's own clock; the controller's sweep uses the exact `created`.
#
# A runner holds a job while its Runner.Worker process exists. Between two jobs it holds none for
# a moment: when jobs are queued, GitHub hands a runner its next job within seconds of the last one
# ending, and the listener first acquires that job and only then starts its Runner.Worker. A
# server deleted in that gap takes the job down with it, and freezing the listener does not close
# the gap: GitHub has already assigned the job, and a frozen listener never starts its worker, so
# the re-check sees nothing. So a runner also counts as busy while any of its worker logs
# (_diag/Worker_*.log, written for the whole of every job) changed in the last QUIET_MIN minutes.
# A runner that has been quiet that long with jobs queued would already have taken one.
#
# The server is deleted only when every runner is known to be idle; a runner whose state cannot
# be read counts as busy.
#
# Stopping a runner service while it holds a job cancels that job ("The runner has received a
# shutdown signal"), so runners are never stopped here. Instead every Runner.Listener is frozen
# with SIGSTOP: a frozen listener cannot take a new job, and freezing does not touch a job that
# is already running. With the listeners frozen the runners are checked again; only if all are
# still idle is the server deleted. In every other case the listeners are thawed and the server
# stays.
#
# A one-job pool (ONE_JOB=1) has ephemeral runners: each takes one job, and its listener exits
# when that job ends. Such a server is deleted as soon as a job has run on it and neither a
# listener nor a worker is left, at any minute: nothing can take a job any more, and the server
# exists for that one job only. Until it has had its job it follows the rule above.
#
# The idle window (POOL_IDLE_KEEP_MIN, default 0 = off): an idle server in its removal window is
# kept for another billed hour while any of its runners ended a job within the last
# POOL_IDLE_KEEP_MIN minutes, so a pipeline that pauses briefly finds a warm server instead of
# waiting for a new one to boot. Billing is per started hour either way; the window only costs
# the hours that end with no job. Each time it keeps the server it labels it pool-keep-until=<epoch>
# so the controller's sweep (pool.py) does not delete it either; once that time passes, both
# apply the plain rule above. A one-job server never uses the window.
set -u
BOOT_LAG=2
QUIET_MIN=${POOL_QUIET_MIN:-3}
IDLE_KEEP_MIN=${POOL_IDLE_KEEP_MIN:-0}
BORN_FILE=${POOL_BORN_FILE:-/etc/pool-born}
RUNNER_ROOT=${POOL_RUNNER_ROOT:-/opt}
METADATA=${POOL_METADATA_URL:-http://169.254.169.254/hetzner/v1/metadata}
HCLOUD_API=${POOL_HCLOUD_API:-https://api.hetzner.cloud/v1}

log() { logger -t pool-reaper "$*"; }

delete_server() {
  local id
  id=$(curl -fsS "$METADATA/instance-id") || { log "minute $minute: cannot read the instance id: keeping server"; exit 0; }
  log "minute $minute: $1: deleting server $id"
  if ! curl -fsS -X DELETE -H "Authorization: Bearer ${HCLOUD_TOKEN}" "$HCLOUD_API/servers/${id}" >/dev/null; then
    log "minute $minute: deleting server $id failed: keeping it"
    exit 1
  fi
}

# Labels the server pool-keep-until=<epoch> beside its pool label, so the controller's sweep
# keeps it too. A failure only means the sweep may delete an idle server early: logged, not fatal.
mark_kept() {
  local id
  if [ -z "${POOL:-}" ]; then log "minute $minute: POOL unknown: cannot label the server kept"; return 0; fi
  id=$(curl -fsS "$METADATA/instance-id") || { log "minute $minute: cannot read the instance id to label it kept"; return 0; }
  curl -fsS -X PUT -H "Authorization: Bearer ${HCLOUD_TOKEN}" -H "Content-Type: application/json" \
    -d "{\"labels\":{\"pool\":\"${POOL}\",\"pool-keep-until\":\"$1\"}}" \
    "$HCLOUD_API/servers/${id}" >/dev/null || log "minute $minute: labelling server $id kept failed"
}

# One-job pool: whether this server's job has run and nothing is left that could take another.
# pgrep exits 1 when nothing matches; any other result counts as "not done".
one_job_done() {
  local logs
  logs=$(find "$RUNNER_ROOT"/actions-runner-*/_diag -maxdepth 1 -name 'Worker_*.log' 2>/dev/null) || true
  [ -n "$logs" ] || return 1
  pgrep -f Runner.Worker >/dev/null; [ $? -eq 1 ] || return 1
  pgrep -f Runner.Listener >/dev/null; [ $? -eq 1 ] || return 1
  return 0
}

born=$(cat "$BORN_FILE") || { log "cannot read $BORN_FILE: keeping server"; exit 0; }
minute=$(( ( ( $(date +%s) - born ) / 60 + BOOT_LAG ) % 60 ))
if [ "${ONE_JOB:-0}" = 1 ] && one_job_done; then
  delete_server "its one job has ended"
  exit 0
fi
[ "$minute" -ge "$REMOVE_FROM" ] && [ "$minute" -lt "$REMOVE_UNTIL" ] || exit 0

slots=()
for d in "$RUNNER_ROOT"/actions-runner-*; do [ -f "$d/.runner" ] && slots+=("$d"); done

runner_name() {
  local name
  name=$(sed -n 's/.*"agentName"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1/.runner" 2>/dev/null | head -n 1)
  echo "${name:-$(basename "$1")}"
}

# busy, recent, idle or unknown for the runner installed in $1. pgrep exits 1 when nothing matches
# and above 1 when it could not look, which is unknown. recent: no job right now, but one ended
# (or the next one is being handed over) within QUIET_MIN minutes.
slot_state() {
  pgrep -f "$1/bin/Runner.Worker" >/dev/null
  case $? in 0) echo busy; return ;; 1) ;; *) echo unknown; return ;; esac
  [ -d "$1/_diag" ] || { echo idle; return; }
  local recent
  recent=$(find "$1/_diag" -maxdepth 1 -name 'Worker_*.log' -mmin "-$QUIET_MIN" 2>/dev/null) || { echo unknown; return; }
  if [ -n "$recent" ]; then echo recent; else echo idle; fi
}

# Logs every runner's state and succeeds only when all are idle. A Runner.Worker that belongs to
# no known runner is a job all the same.
all_idle() {
  local d state ok=0
  for d in "${slots[@]+"${slots[@]}"}"; do
    state=$(slot_state "$d")
    log "minute $minute ($1): runner $(runner_name "$d") busy=$state"
    [ "$state" = idle ] || ok=1
  done
  pgrep -f Runner.Worker >/dev/null
  case $? in
    0) [ "$ok" -ne 0 ] || log "minute $minute ($1): a Runner.Worker of no known runner is running"; ok=1 ;;
    1) ;;
    *) log "minute $minute ($1): cannot list processes"; ok=1 ;;
  esac
  return $ok
}

all_idle check || { log "minute $minute: a runner is busy, ended a job moments ago, or its state is unknown: keeping server"; exit 0; }

# The idle window: a job ended within the last IDLE_KEEP_MIN minutes, so work is likely to come.
if [ "${ONE_JOB:-0}" != 1 ] && [ "$IDLE_KEEP_MIN" -gt 0 ]; then
  recent=$(find "$RUNNER_ROOT"/actions-runner-*/_diag -maxdepth 1 -name 'Worker_*.log' \
    -mmin "-$IDLE_KEEP_MIN" 2>/dev/null | head -n 1)
  if [ -n "$recent" ]; then
    # Until the next decision at most: this reaper decides again in the next hour's window.
    keep_until=$(( $(date +%s) + IDLE_KEEP_MIN * 60 ))
    log "minute $minute: idle, but a job ended within the last $IDLE_KEEP_MIN minutes: keeping server until $keep_until"
    mark_kept "$keep_until"
    exit 0
  fi
fi

frozen=()
thaw() { [ ${#frozen[@]} -eq 0 ] || kill -CONT "${frozen[@]}" 2>/dev/null; frozen=(); }
trap thaw EXIT

for d in "${slots[@]+"${slots[@]}"}"; do
  pids=$(pgrep -f "$d/bin/Runner.Listener")
  rc=$?
  if [ $rc -gt 1 ]; then log "minute $minute: cannot find the listener of $(runner_name "$d"): keeping server"; exit 0; fi
  for p in $pids; do
    kill -STOP "$p" || { log "minute $minute: cannot freeze listener $p: keeping server"; exit 0; }
    frozen+=("$p")
  done
done

all_idle frozen || { log "minute $minute: a job started before the listeners froze: keeping server"; exit 0; }

delete_server "every runner idle"
# Deletion is under way; the listeners stay frozen so no job lands on a server that is going.
frozen=()
