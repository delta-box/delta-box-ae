"""Capture Cube's effective template and executable inputs before measurement."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
from urllib.request import urlopen


def _positive_integer(value: Decimal, name: str) -> int:
    if not value.is_finite() or value <= 0 or value != value.to_integral_value():
        raise ValueError(f'Cube {name} must be a positive whole number')
    return int(value)


def _cpu_millicores(value: object) -> int:
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(m?)', str(value))
    if not match:
        raise ValueError('Cube template CPU is missing or unrecognized')
    amount = Decimal(match[1]) * (1 if match[2] else 1000)
    return _positive_integer(amount, 'CPU millicores')


def _memory_mib(value: object) -> int:
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T|B)?', str(value))
    if not match:
        raise ValueError('Cube template memory is missing or unrecognized')
    units = {'': 1, 'B': 1, 'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3,
             'Ti': 1024**4, 'K': 1000, 'M': 1000**2, 'G': 1000**3, 'T': 1000**4}
    amount = Decimal(match[1]) * units[match[2] or ''] / (1024**2)
    return _positive_integer(amount, 'memory MiB')


def _file_record(path: Path, *, name: str | None = None) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return {'path': name if name is not None else str(path.resolve()),
            'size_bytes': path.stat().st_size, 'sha256': digest.hexdigest()}


def _template_record(detail: dict, template: str) -> dict:
    if detail.get('templateID') != template:
        raise ValueError('Cube API returned a different template identity')
    if detail.get('status') != 'READY':
        raise ValueError(f'Cube template {template} is not READY')
    containers = detail.get('createRequest', {}).get('containers', [])
    if len(containers) != 1:
        raise ValueError('Cube benchmark requires one container with explicit template resources')
    container = containers[0]
    resources = container.get('resources', {})
    cpu = _cpu_millicores(resources.get('cpu'))
    memory = _memory_mib(resources.get('mem'))
    image = container.get('image', {})
    annotations = image.get('annotations', {})
    # Template responses contain environment variables and download tokens.
    # Select identity fields explicitly; never archive the complete response.
    return {
        'template_cpu_millicores': cpu,
        'template_memory_mb': memory,
        'memory_unit': 'MiB',
        'template': {
            'template_id': template, 'status': detail['status'],
            'version': detail.get('version'), 'instance_type': detail.get('instanceType'),
            'resources': {'cpu': resources['cpu'], 'mem': resources['mem']},
            'replica_specs': [row.get('spec') for row in detail.get('replicas', [])],
            'image': image.get('image'), 'storage_media': image.get('storage_media'),
            'writable_layer_size': image.get('writable_layer_size'),
            'artifact_id': annotations.get('cube.master.rootfs.artifact.id'),
            'artifact_sha256': annotations.get('cube.master.rootfs.artifact.sha256'),
            'artifact_size_bytes': annotations.get('cube.master.rootfs.artifact.size_bytes'),
        },
    }


def capture_cube_environment(*, api_url: str, template: str, sdk_path: Path,
                             phase_binary: Path, timeout: float = 10) -> dict:
    """Return sanitized live-template resources and hashes, outside timed work.

    ``template_memory_mb`` is the historical driver's option name; its value is
    measured in MiB. Caller must pass these actual values to that driver rather
    than letting its unrelated defaults label a run.
    """
    parsed = urlsplit(api_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('Cube API URL must be HTTP(S)')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Cube API URL must not contain credentials, query, or fragment')
    endpoint = api_url.rstrip('/') + '/templates/' + quote(template, safe='')
    with urlopen(endpoint, timeout=timeout) as response:
        detail = json.load(response)
    if not isinstance(detail, dict):
        raise ValueError('Cube template response must be an object')
    result = _template_record(detail, template)
    sdk_path = Path(sdk_path).resolve()
    package = sdk_path / 'cubesandbox'
    if not (package / '__init__.py').is_file():
        raise FileNotFoundError(package / '__init__.py')
    files = [_file_record(path, name=path.relative_to(sdk_path).as_posix())
             for path in sorted(package.rglob('*.py'))]
    result.update(
        captured_at=datetime.now(timezone.utc).isoformat(),
        api_url=api_url.rstrip('/'),
        resource_source='GET template createRequest.containers[0].resources',
        sdk={'root': str(sdk_path), 'python_files': files,
             'manifest_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()},
        cubelet_binary=_file_record(Path(phase_binary)),
    )
    return result
