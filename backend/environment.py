"""Allowlisted environments for inventory discovery and job execution."""

import os
from pathlib import Path


def execution_environment(directory):
    """Keep ambient credentials and Ansible overrides out of child processes."""
    directory = Path(directory)
    runner_home = directory / "home"
    runner_temp = directory / "tmp"
    for path in (runner_home, runner_temp):
        path.mkdir(mode=0o700)
    # shortcut: PATH is the trusted local toolchain; pin worker images before remote execution.
    return {
        **{
            name: os.environ[name]
            for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE")
            if name in os.environ
        },
        "HOME": str(runner_home),
        "TMPDIR": str(runner_temp),
        "ANSIBLE_HOME": str(runner_home / ".ansible"),
        "ANSIBLE_LOCAL_TEMP": str(runner_temp / "ansible"),
        "ANSIBLE_REMOTE_TEMP": str(runner_temp / "modules"),
        "ANSIBLE_CONFIG": str(directory / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "PYTHONUNBUFFERED": "1",
    }
