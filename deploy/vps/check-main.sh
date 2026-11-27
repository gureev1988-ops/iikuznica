#!/usr/bin/env bash
# Poll public GitHub CI on the VPS. The server keeps all deploy credentials.
set -euo pipefail
umask 077

root=${II_DEPLOY_ROOT:-/opt/iikuznica}
repo=$root/repo
upstream=https://github.com/gureev1988-ops/iikuznica.git
if [[ $(git -C "$repo" remote get-url origin) != "$upstream" ]]; then
    printf '%s\n' 'Deployment repository has an unexpected origin' >&2
    exit 65
fi
mkdir -p "$root/releases"
exec 9>"$root/deploy.lock"
flock -n 9 || exit 0

git -C "$repo" fetch --quiet --no-tags origin '+refs/heads/main:refs/remotes/origin/main'
revision=$(git -C "$repo" rev-parse refs/remotes/origin/main)
current=$root/shared/current
if [[ -s $current && $(<"$current") == "$revision" && ${II_FORCE_DEPLOY:-0} != 1 ]]; then
    exit 0
fi

endpoint=https://api.github.com/repos/gureev1988-ops/iikuznica/actions/workflows/core-checks.yml/runs
runs=$(curl -fsS --connect-timeout 5 --max-time 20 --retry 1 \
    -H 'Accept: application/vnd.github+json' \
    "$endpoint?branch=main&head_sha=$revision&per_page=10")
status=$(python3 -c '
import json
import sys
result = json.load(sys.stdin)
for run in result["workflow_runs"]:
    if (run["head_sha"] == sys.argv[1] and run["head_branch"] == "main"
            and run["event"] in {"push", "workflow_dispatch"}):
        print(run["conclusion"] if run["status"] == "completed" else "pending")
        break
else:
    print("missing")
' "$revision" <<<"$runs")
if [[ $status != success ]]; then
    printf 'Waiting for green CI on %s (%s)\n' "$revision" "$status"
    exit 0
fi

# A new main commit can arrive while the REST request is in flight.
tip=$(git -C "$repo" ls-remote origin refs/heads/main)
if [[ ${tip%%$'\t'*} != "$revision" ]]; then
    exit 0
fi

release=$root/releases/$revision
if [[ ! -d $release ]]; then
    temp=$(mktemp -d "$root/releases/.extract.XXXXXXXX")
    trap 'rm -rf -- "$temp"' EXIT
    git -C "$repo" archive "$revision" | tar -x -C "$temp"
    mv -- "$temp" "$release"
    trap - EXIT
fi
exec bash "$release/deploy/vps/apply.sh" "$root" "$revision"
