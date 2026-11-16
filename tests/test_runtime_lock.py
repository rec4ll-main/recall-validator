"""Exercise the real locked exporter against disposable project copies."""
import importlib.util
import os
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / 'scripts' / 'check_runtime_lock.py'


@pytest.fixture
def project(tmp_path):
    for name in ('pyproject.toml', 'uv.lock', 'requirements.lock', 'README.md'):
        shutil.copyfile(ROOT / name, tmp_path / name)
    return tmp_path


def check(project):
    assert CHECK.is_file(), 'runtime export drift check must be committed'
    if not shutil.which('uv'):
        if os.environ.get('CI'):
            pytest.fail('CI requires the pinned uv exporter')
        pytest.skip('uv is required for the real export regression')
    spec = importlib.util.spec_from_file_location('runtime_lock_check', CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_runtime_lock(project)


def test_matching_export(project, tmp_path):
    check(project)


@pytest.mark.parametrize('mutation', ['version', 'hash', 'manifest'])
def test_stale_export_refused_without_rewriting(project, tmp_path, mutation):
    target = project / ('pyproject.toml' if mutation == 'manifest' else 'requirements.lock')
    text = target.read_text()
    if mutation == 'version':
        text = text.replace('aiohappyeyeballs==2.6.1', 'aiohappyeyeballs==0.0.0', 1)
    elif mutation == 'hash':
        text = text.replace('sha256:', 'sha256:0', 1)
    else:
        text = text.replace('bittensor==10.5.0', 'bittensor==0.0.0', 1)
    target.write_text(text)
    before = {name: (project / name).read_bytes() for name in ('pyproject.toml', 'uv.lock', 'requirements.lock')}
    with pytest.raises(RuntimeError):
        check(project)
    assert before == {name: (project / name).read_bytes() for name in before}


def test_ci_checks_export_before_install():
    workflow = (ROOT / '.github/workflows/ci.yml').read_text()
    command = 'uv run --no-project --python 3.12 python scripts/check_runtime_lock.py'
    assert command in workflow, 'CI must reject stale runtime exports before installation'
    assert workflow.index(command) < workflow.index('uv sync --frozen')
    assert 'version: "0.10.10"' in workflow
