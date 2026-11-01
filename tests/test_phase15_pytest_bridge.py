"""Run legacy standalone regression scripts under pytest without changing them.

These scripts remain isolated subprocesses, so their original __main__ checks run.
"""
from pathlib import Path
import subprocess
import sys

import pytest

TEST_DIR = Path(__file__).resolve().parent
LEGACY_TESTS = sorted(p for p in TEST_DIR.glob('test_*.py') if p.name != Path(__file__).name)


@pytest.mark.parametrize('script', LEGACY_TESTS, ids=lambda path: path.stem)
def test_legacy_regression_script(script):
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(TEST_DIR.parent),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f'{script.name} failed with exit code {result.returncode}\n'
        f'STDOUT:\n{result.stdout[-4000:]}\nSTDERR:\n{result.stderr[-4000:]}'
    )
