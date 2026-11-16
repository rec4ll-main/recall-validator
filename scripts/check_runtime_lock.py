"""Fail if the runtime requirements differ from the existing validated uv lock.

Uses the CI-pinned uv exporter, offline and locked: never refreshes either file.
Run from Python 3.12, matching the project's supported interpreter.
"""
from pathlib import Path
import subprocess
import sys


def check_runtime_lock(root: Path) -> None:
    try:
        result = subprocess.run(
            ['uv', 'export', '--offline', '--locked', '--no-dev',
             '--no-emit-project', '--no-header', '--no-python-downloads',
             '--python', sys.executable, '--project', str(root)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('runtime lock check could not run the pinned uv exporter') from exc
    if result.returncode:
        raise RuntimeError('uv.lock does not validate against the project; locked offline export failed')
    if result.stdout != (root / 'requirements.lock').read_text():
        raise RuntimeError('requirements.lock differs from the validated uv.lock export')


def main() -> int:
    try:
        check_runtime_lock(Path(__file__).resolve().parents[1])
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print('Runtime lock export matches uv.lock')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
