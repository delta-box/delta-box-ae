"""Stage the retained historical repository search for the paper E2B profile.

The generic deterministic replay search filters the complete glob. The original
Moatless method instead searches the directory prefix before ** using grep.
That difference changes real action observations; this adapter restores the
retained search scope and parsing, with sorted live directory traversal (parent files before subdirectories) for
deterministic traversal. No recorded result or file-priority table is consulted.
Only a private job payload is edited. The shared source stays unchanged.
"""
from __future__ import annotations
import ast
import hashlib
from pathlib import Path
import shutil
import textwrap

HISTORICAL_COMMIT = 'bc66ea64e3fe96cf3c90f197b4bab9aaa15e1eb9'
HISTORICAL_FUNCTION_SHA256 = '44143c2e8634ff331beeb2c5e60b404e8cdecca42b5a3479f8e3188642c4db9c'

HISTORICAL_METHOD = r'''def find_exact_matches(
    self, search_text: str, file_pattern: Optional[str] = None
) -> List[tuple[str, int]]:
    """
    Uses grep to search for exact text matches in files.
    """
    matches = []
    if not file_pattern:
        file_pattern = "."

    try:
        # Remove '**' and everything after it
        grep_pattern = file_pattern
        if "**" in grep_pattern:
            grep_pattern = grep_pattern.split("**")[0]

        if not grep_pattern:
            grep_pattern = "."

        # Always escape special regex characters to handle them literally
        escaped_search_text = (
            search_text.replace("[", "\\[")
            .replace("]", "\\]")
            .replace(".", "\\.")
            .replace("+", "\\+")
            .replace("*", "\\*")
            .replace("?", "\\?")
            .replace("|", "\\|")
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace("$", "\\$")
            .replace("^", "\\^")
        )

        cmd = ["grep", "-n", "-r", escaped_search_text, grep_pattern]
        logger.info(f"Executing grep command: {' '.join(cmd)}")
        logger.info(f"Search directory: {self.repo_path}")

        result = subprocess.run(
            cmd, cwd=self.repo_path, capture_output=True, text=True
        )

        if result.returncode not in (0, 1):  # grep returns 1 if no matches found
            logger.info(
                f"Grep returned non-standard exit code: {result.returncode}"
            )
            if result.stderr:
                logger.warning(f"Grep error output: {result.stderr}")
            return []

        logger.info(f"Found {len(result.stdout.splitlines())} potential matches")

        for line in result.stdout.splitlines():
            try:
                parts = line.split(":", 2)
                if len(parts) < 2:
                    logger.info(f"Skipping malformed line: {line}")
                    continue

                if (
                    os.path.isfile(os.path.join(self.repo_path, file_pattern))
                    and "/" not in parts[0]
                ):
                    # Format: "5:def test_partitions():"
                    line_num = int(parts[0])
                    content = parts[1]
                    file_path = file_pattern
                else:
                    # Format: "path/to/file:5:def test_partitions():"
                    file_path = parts[0]
                    if file_path.startswith("./"):
                        file_path = file_path[2:]
                    line_num = int(parts[1])
                    content = parts[2]

                matches.append((file_path, int(line_num)))
            except (ValueError, IndexError) as e:
                logger.info(f"Error parsing line '{line}': {e}")
                continue

    except subprocess.SubprocessError as e:
        logger.info(f"Grep command failed: {e}")
        return []

    logger.info(f"Returning {len(matches)} matches")
    return matches
'''


def stage_historical_search(payload: Path) -> dict:
    payload = Path(payload)
    package = payload / 'moatless-det-src'
    original_package = package.resolve(strict=True)
    relative = Path('moatless/repository/file.py')
    original_file = original_package / relative
    before = original_file.read_bytes()
    source = before.decode('utf-8')
    tree = ast.parse(source)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'FileRepository']
    methods = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == 'find_exact_matches'] if len(classes) == 1 else []
    if len(methods) != 1:
        raise ValueError('Expected one FileRepository.find_exact_matches method')
    method = methods[0]
    lines = source.splitlines(keepends=True)
    final_return = '    return matches\n'
    if not HISTORICAL_METHOD.endswith(final_return):
        raise ValueError('Historical search function boundary differs')
    ordered_return = (
        '    def traversal_order(match):\n'
        '        parts = match[0].split("/")\n'
        '        path_order = tuple((1, part) for part in parts[:-1]) + ((0, parts[-1]),)\n'
        '        return path_order, match[1]\n'
        '    return sorted(matches, key=traversal_order)\n'
    )
    implementation = HISTORICAL_METHOD[:-len(final_return)] + ordered_return
    replacement = textwrap.indent(implementation, ' ' * method.col_offset)
    updated = ''.join(lines[:method.lineno-1]) + replacement + ''.join(lines[method.end_lineno:])
    compile(updated, str(original_file), 'exec')
    if package.is_symlink():
        staged = payload / 'moatless-det-src.paper-search-copy'
        shutil.copytree(original_package, staged, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
        if original_file.read_bytes() != before:
            raise ValueError('Shared source changed while staging historical search')
        package.unlink()
        staged.rename(package)
    target = package / relative
    target.write_text(updated)
    if original_package != package.resolve() and original_file.read_bytes() != before:
        raise ValueError('Shared source changed during private adaptation')
    return {
        'kind': 'e2b-paper-historical-grep-v1',
        'scope': 'Private paper-profile payload; original grep scope/parsing with sorted live matches; no recorded results or priorities',
        'historical_commit': HISTORICAL_COMMIT,
        'historical_source_path': 'moatless/repository/file.py',
        'historical_function_sha256': hashlib.sha256(HISTORICAL_METHOD.encode()).hexdigest(),
        'staged_function_sha256': hashlib.sha256(implementation.encode()).hexdigest(),
        'ordering': 'Deterministic os.walk order: sorted parent files before sorted subdirectories; retain all live matches and duplicates',
        'source_path': str(original_file),
        'staged_path': str(target),
        'before_sha256': hashlib.sha256(before).hexdigest(),
        'after_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'shared_source_unchanged': original_file.read_bytes() == before if original_package != package.resolve() else None,
        'historical_deployed_source_sha_unconfirmed': True,
    }
