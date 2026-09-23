#!/usr/bin/env python3
"""Fetch the pinned public Linux installer used by the master image recipe."""
import argparse
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen

# Publisher index: https://repo.anaconda.com/miniconda/
# This is a reconstruction dependency, not a recovered historical image lock.
URL = 'https://repo.anaconda.com/miniconda/Miniconda3-py311_24.5.0-0-Linux-x86_64.sh'
SHA256 = '38b203bb1f2be78b735ebc00162f29e8e73fcd9a619ed5980490a72193ee1f58'


def fetch(output):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'Refusing existing installer: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            with urlopen(URL, timeout=60) as response:
                for block in iter(lambda: response.read(4 << 20), b''):
                    stream.write(block)
                    digest.update(block)
        if digest.hexdigest() != SHA256:
            raise ValueError('Downloaded Miniconda installer differs from the pinned release')
        # Publish only verified bytes, without replacing a concurrent writer.
        os.link(temporary, output)
        return SHA256
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        print(f'Downloading Miniconda from {URL}', file=sys.stderr)
        print(fetch(args.output))
    except (OSError, ValueError) as error:
        parser.exit(1, f'Installer download failed: {error}\n')
