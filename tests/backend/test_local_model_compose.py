"""Check the effective Compose mapping without starting containers or contacting a model."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
APPLICATION = ('init', 'api', 'interactive', 'background')
LOCAL = ['--env-file', 'deploy/models/local.env.example', '-f', 'deploy/models/compose.local.yaml']


@pytest.fixture
def compose(tmp_path):
    if not shutil.which('docker'):
        pytest.skip('Docker Compose v2 is required to render the deployment configuration')
    env_file = tmp_path / 'assembly.env'
    env_file.write_text(''.join(f'II_{name}=fixture\n' for name in (
        'DB_PASSWORD', 'JUDGE_LOGIN', 'JUDGE_PASSWORD', 'QUALITY_PASSWORD', 'BOT_PASSWORD', 'IMAP_OPERATOR_PASSWORD',
    )))
    env = {'PATH': os.environ['PATH'], 'HOME': os.environ['HOME'], 'OPENROUTER_API_KEY': 'demo-fixture'}

    def render(*flags, **settings):
        return subprocess.run(['docker', 'compose', '--env-file', str(env_file), '-f', 'compose.yaml', *flags,
                               'config', '--format', 'json'], cwd=ROOT, env={**env, **settings},
                              capture_output=True, text=True)

    return render


def test_local_override_shares_routing_and_readonly_minilm(compose):
    token_settings = {f'II_LOCAL_{role}_TOKEN': f'{role.lower()}-fixture' for role in ('GEMMA', 'QWEN', 'DECIDER')}
    local = compose(*LOCAL, **token_settings)
    local.check_returncode()
    services = json.loads(local.stdout)['services']
    mapping = services['api']['environment']
    for service in APPLICATION:
        environment = services[service]['environment']
        assert environment['II_MODEL_PROFILE'] == 'local'
        assert not environment.get('OPENROUTER_API_KEY')
        for role in ('GEMMA', 'QWEN', 'DECIDER'):
            for key in ('URL', 'MODEL', 'TOKEN'):
                name = f'II_LOCAL_{role}_{key}'
                assert environment[name] == mapping[name]
            assert mapping[f'II_LOCAL_{role}_URL'].startswith('https://' + role.lower() + '.')
            assert mapping[f'II_LOCAL_{role}_MODEL']
            assert mapping[f'II_LOCAL_{role}_TOKEN'] == token_settings[f'II_LOCAL_{role}_TOKEN']
        mounts = {v['target']: v for v in services[service]['volumes']}
        assert mounts['/models']['read_only'] and mounts['/models']['type'] == 'bind'
        assert mounts['/models']['source'] == '/srv/iikuznica/models/minilm'
        assert mounts['/originals']['type'] == 'volume'
    for service in set(services) - set(APPLICATION):
        assert 'II_LOCAL_GEMMA_URL' not in services[service].get('environment', {})


def test_local_override_rejects_missing_endpoint_and_demo_still_runs(compose):
    missing = compose(*LOCAL, II_LOCAL_DECIDER_URL='')
    assert missing.returncode and 'II_LOCAL_DECIDER_URL' in missing.stderr
    demo = compose('-f', 'deploy/vps/compose.production.yaml', II_RELEASE_SHA='a' * 40)
    demo.check_returncode()
    services = json.loads(demo.stdout)['services']
    for service in ('api', 'interactive', 'background'):
        env = services[service]['environment']
        assert env.get('II_MODEL_PROFILE', 'openrouter') == 'openrouter'
        assert env['OPENROUTER_API_KEY'] == 'demo-fixture'
        assert 'II_LOCAL_DECIDER_URL' not in env


def test_gpu_profile_uses_prepared_images_and_weights_behind_private_ingress():
    if not shutil.which('docker'):
        pytest.skip('Docker Compose v2 is required to render the deployment configuration')
    directory = ROOT / 'deploy' / 'local-inference'
    env = {'PATH': os.environ['PATH'], 'HOME': os.environ['HOME'], 'II_DECIDER_IMAGE_REPOSITORY': 'test/decider',
           'II_DECIDER_IMAGE_SHA256': 'a' * 64, **{f'II_{role}_API_KEY': 'fixture' for role in ('GEMMA', 'QWEN', 'DECIDER')}}
    rendered = subprocess.check_output(['docker', 'compose', '--env-file', 'pilot.env.example', '-f', 'compose.yaml',
                                        'config', '--format', 'json'], cwd=directory, env=env)
    config = json.loads(rendered)
    assert config['networks']['inference']['internal']
    for role in ('gemma', 'qwen', 'decider'):
        service = config['services'][role]
        assert set(service['networks']) == {'inference'} and not service.get('ports')
        assert service['gpus'] and service['pull_policy'] == 'never' and '@sha256:' in service['image']
        mount, = service['volumes']
        assert mount['read_only'] and mount['target'] == '/models/' + role
    ports = config['services']['ingress']['ports']
    assert {(port['host_ip'], port['target']) for port in ports} == {('127.0.0.1', n) for n in (18081, 18082, 18083)}
