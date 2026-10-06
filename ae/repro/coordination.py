"""Locate shared hosted admission state without relocating experiment data.

The hosted launcher discards the caller environment and sets this override only
from its root-owned policy after validating the directory. Unconfigured and
non-hosted runs retain their repository-local coordination paths.
"""
import os
from pathlib import Path


COORDINATION_ENV = 'AE_HOSTED_COORDINATION_ROOT'


def coordination_root(repo_root: Path) -> Path:
    value = os.environ.get(COORDINATION_ENV)
    if value is None:
        return Path(repo_root) / 'ae/work'
    path = Path(value)
    if not value or not path.is_absolute() or '..' in path.parts or '\x00' in value:
        raise ValueError('Hosted coordination root must be an absolute path without parent traversal')
    return path
