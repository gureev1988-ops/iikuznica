"""Check deployment ordering, supersession and rollback without touching a real host."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "vps"
OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def deployment(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "hostname").write_text("demo.example.org\n")
    (shared / "production.env").touch()
    for revision in (OLD, NEW):
        release = tmp_path / "releases" / revision
        (release / "deploy" / "vps").mkdir(parents=True)
        (release / "deploy" / "vps" / "check-mail.py").touch()
        shutil.copyfile(SCRIPTS / "apply.sh", release / "deploy" / "vps" / "apply.sh")
        (release / "deploy" / "qa").mkdir()
        (release / "deploy" / "qa" / "models.py").touch()
    binary = tmp_path / "bin"
    binary.mkdir()

    def install(name, script):
        path = binary / name
        path.write_text("#!/bin/bash\nset -eu\n" + script)
        path.chmod(0o700)

    install("docker", '''
        printf '%s %s\\n' "$II_RELEASE_SHA" "$*" >>"$TEST_VPS_ROOT/calls"
        case " $* " in
            *" run "*) [[ ${TEST_FAIL_MODE:-} != preflight ]] ;;
            *" pg_dump "*) printf 'trusted test database dump' ;;
            *" up "*) printf '%s\\n' "$II_RELEASE_SHA" >"$TEST_VPS_ROOT/active" ;;
            *" api python "*) [[ ${TEST_FAIL_MODE:-} != postflight ]] ;;
        esac
    ''')
    install("curl", '''
        if [[ $* == *api.github.com* ]]; then
            cat "$TEST_VPS_ROOT/workflow-runs.json"
            exit 0
        fi
        printf 'HTTP/2 200\\r\\nX-IIKuznica-Revision: %s\\r\\n\\r\\n' "$(cat "$TEST_VPS_ROOT/active")"
    ''')
    install("git", '''
        case "$*" in
            *" remote get-url origin") printf 'https://github.com/gureev1988-ops/iikuznica.git\\n' ;;
            *" fetch "*) exit 0 ;;
            *" rev-parse refs/remotes/origin/main") printf '%s\\n' "$TEST_TIP" ;;
            *" ls-remote origin refs/heads/main") printf '%s\\trefs/heads/main\\n' "${TEST_CHANGED_TIP:-$TEST_TIP}" ;;
            *) exit 65 ;;
        esac
    ''')
    env = dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}",
               II_DEPLOY_ROOT=str(tmp_path), TEST_VPS_ROOT=str(tmp_path), TEST_TIP=NEW)
    return tmp_path, env


def run(script, env, *args):
    return subprocess.run(["bash", str(SCRIPTS / script), *args], env=env, capture_output=True, text=True, timeout=10)


def test_successful_deployment_marks_revision_and_keeps_backup(deployment):
    root, env = deployment
    (root / "shared" / "current").write_text(OLD)
    result = run("apply.sh", env, str(root), NEW)
    assert result.returncode == 0, result.stderr
    assert f"deployed {NEW}" in result.stdout
    assert (root / "shared" / "current").read_text().strip() == NEW
    (backup,) = (root / "backups").glob("*.dump")
    assert backup.read_text() == "trusted test database dump"
    calls = (root / "calls").read_text()
    assert calls.index("run --no-deps") < calls.index("pg_dump") < calls.index("up -d") < calls.index("exec -T api")


@pytest.mark.parametrize("failure", ["preflight", "postflight"])
def test_failed_deployment_keeps_previous_revision(deployment, failure):
    root, env = deployment
    (root / "shared" / "current").write_text(OLD)
    (root / "active").write_text(OLD)
    result = run("apply.sh", dict(env, TEST_FAIL_MODE=failure), str(root), NEW)
    assert result.returncode != 0
    assert (root / "shared" / "current").read_text() == OLD
    assert (root / "active").read_text().strip() == OLD
    calls = (root / "calls").read_text()
    if failure == "preflight":
        assert "up -d" not in calls
    else:
        assert "up -d --no-build --no-deps" in calls


def workflow_run(*, revision=NEW, branch="main", event="push", status="completed", conclusion="success"):
    return {"head_sha": revision, "head_branch": branch, "event": event, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize("runs", [
    [],
    [workflow_run(revision=OLD)],
    [workflow_run(branch="feature")],
    [workflow_run(event="pull_request")],
    [workflow_run(status="in_progress", conclusion=None), workflow_run()],
    [workflow_run(conclusion="failure"), workflow_run()],
])
def test_timer_leaves_previous_release_until_exact_main_is_green(deployment, runs):
    root, env = deployment
    (root / "workflow-runs.json").write_text(json.dumps({"workflow_runs": runs}))
    result = run("check-main.sh", env)
    assert result.returncode == 0, result.stderr
    assert f"Waiting for green CI on {NEW}" in result.stdout
    assert not (root / "calls").exists()


def test_timer_skips_superseded_sha_and_deploys_green_tip_once(deployment):
    root, env = deployment
    (root / "workflow-runs.json").write_text(json.dumps({"workflow_runs": [workflow_run()]}))
    result = run("check-main.sh", dict(env, TEST_CHANGED_TIP=OLD))
    assert result.returncode == 0
    assert not (root / "calls").exists()
    result = run("check-main.sh", env)
    assert result.returncode == 0, result.stderr
    assert f"deployed {NEW}" in result.stdout
    log = (root / "calls").read_text()
    assert run("check-main.sh", env).returncode == 0
    assert (root / "calls").read_text() == log
