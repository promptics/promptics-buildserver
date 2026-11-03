#!/bin/bash
# The job-started hook of a one-job pool's runner (ACTIONS_RUNNER_HOOK_JOB_STARTED). The runner
# runs it before the job's first step, with the job's GITHUB_* variables; a non-zero exit fails
# the job, so none of its steps run.
#
# A one-job server may give its job more than a shared pool server does (a privileged container,
# for one), so it takes only the jobs it was created for: runs started by one of EVENTS, in one of
# ALLOWED_REPOS of ORG, on the main branch. The scaler only creates such a server for those jobs,
# but GitHub hands a job to any idle runner whose labels match, so the runner checks again itself.
#
# A workflow_run always runs on the default branch, whatever started it, so for that event the
# guard also reads the event payload (GITHUB_EVENT_PATH) and takes the job only when the run that
# triggered it was on main of the same repository. A payload it cannot read refuses the job.
set -u
GUARD_ENV=${POOL_GUARD_ENV:-/etc/pool-job-guard.env}
# shellcheck disable=SC1090
. "$GUARD_ENV" 2>/dev/null || { echo "pool-job-guard: cannot read $GUARD_ENV: refusing the job" >&2; exit 1; }

event=${GITHUB_EVENT_NAME:-}
repository=${GITHUB_REPOSITORY:-}

allowed_event=1
for e in ${EVENTS:-}; do [ "$e" = "$event" ] && allowed_event=0; done
if [ -z "$event" ] || [ "$allowed_event" -ne 0 ]; then
  echo "pool-job-guard: this runner takes only jobs of: ${EVENTS:-nothing}; refusing a job of event '${event}'" >&2
  exit 1
fi

allowed_repo=1
for r in ${ALLOWED_REPOS:-}; do [ "${ORG:-}/$r" = "$repository" ] && allowed_repo=0; done
if [ -z "$repository" ] || [ "$allowed_repo" -ne 0 ]; then
  echo "pool-job-guard: this runner takes no job of repository '${repository}'; refusing it" >&2
  exit 1
fi

# A manual start may name any branch, and the workflow is then read from that branch, so an
# unreviewed edit would run here. Only runs of ALLOWED_REF (default: main) start.
ref=${GITHUB_REF:-}
allowed_ref=${ALLOWED_REF:-refs/heads/main}
if [ "$ref" != "$allowed_ref" ]; then
  echo "pool-job-guard: this runner takes only runs of $allowed_ref; refusing a run of '${ref}'" >&2
  exit 1
fi

# GITHUB_REF of a workflow_run is the default branch, not the branch of the run that triggered
# it: that one is in the payload.
if [ "$event" = workflow_run ]; then
  trigger=$(python3 -I - "${GITHUB_EVENT_PATH:-}" <<'PY' 2>/dev/null
import json, sys
run = json.load(open(sys.argv[1]))["workflow_run"]
print(run.get("head_branch") or "", (run.get("head_repository") or {}).get("full_name") or "")
PY
  ) || { echo "pool-job-guard: cannot read the workflow_run payload at '${GITHUB_EVENT_PATH:-}': refusing the job" >&2; exit 1; }
  read -r head_branch head_repository <<<"$trigger"
  if [ "refs/heads/${head_branch:-}" != "$allowed_ref" ] || [ "${head_repository:-}" != "$repository" ]; then
    echo "pool-job-guard: this runner takes a workflow_run only after a run of $allowed_ref in $repository; refusing one after '${head_branch:-}' in '${head_repository:-}'" >&2
    exit 1
  fi
fi

echo "pool-job-guard: $event job of $repository on $ref accepted"
