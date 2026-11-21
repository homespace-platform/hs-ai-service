#!/usr/bin/env python3
"""Validate and sync HomeSpace repository skills using a local toolkit checkout."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile


LOCK = '.agents/skills.lock.json'
BEGIN = '<!-- BEGIN: HomeSpace engineering standards -->'
END = '<!-- END: HomeSpace engineering standards -->'
NAME = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
SHA = re.compile(r'[0-9a-f]{40}')
DIGEST = re.compile(r'[0-9a-f]{64}')
ABSOLUTE = re.compile(r'(?i)(?:\b[A-Z]:[\\/]|\\\\[\w.-]+\\|(?:~|\$HOME|\$\{HOME\}|%USERPROFILE%)[\\/]|(?<![\w.])/(?:Users|home)/)')
SHARED_FILES = {
    '@standards': 'standards.md',
    'scripts/ai-skills.py': 'scripts/ai-skills.py',
    '.github/workflows/ai-skills.yml': 'templates/ai-skills.yml',
    'docs/ai/skills.md': 'templates/ai-skills.md',
}


def path_at(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or '..' in parts or '\\' in relative or ':' in relative:
        raise ValueError(f'Invalid relative path: {relative}')
    path = root
    for index, part in enumerate(parts):
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f'Symlink or reparse point: {relative}')
        expected = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
        if not expected(info.st_mode):
            raise ValueError(f'Unexpected file type: {relative}')
    return path


def normalize(raw):
    value = raw.decode('utf-8').replace('\r\n', '\n')
    if value.startswith('\ufeff') or '\r' in value or '\x00' in value:
        raise ValueError('Expected UTF-8 text without BOM, bare CR or NUL')
    return value


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def parse_skill(value, name):
    # shortcut: the catalog uses one-line YAML scalars, add a YAML parser if richer metadata is needed
    match = re.fullmatch(r'---\n(name: [^\n]+\ndescription: [^\n]+)\n---\n(.*)', value, re.S)
    if not match or match[1].splitlines()[0] != f'name: {name}' or not match[2].strip():
        raise ValueError(f'{name}: expected matching name, one-line description and a workflow body')
    description = match[1].splitlines()[1].removeprefix('description: ').strip()
    if (not description or len(description) > 1024 or description.startswith(('[', '{', '>', '|', '#', '&', '*', '!', '"', "'"))
            or ': ' in description or ' #' in description or description.endswith(':')
            or description.lower() in {'true', 'false', 'null', '~'} or re.fullmatch(r'[+-]?[\d.]+', description)):
        raise ValueError(f'{name}: description must be a plain YAML string of at most 1024 characters')
    if ABSOLUTE.search(value) or re.search(r'\.\.[/\\].*SKILL\.md', value, re.I):
        raise ValueError(f'{name}: absolute machine path or skill adapter')
    return match[1], match[2]


def manual_only(value, name):
    if value is None:
        return False
    match = re.search(r'^policy:\n  allow_implicit_invocation: (true|false)$', value, re.M)
    if not match or value.count('allow_implicit_invocation:') != 1 or ABSOLUTE.search(value):
        raise ValueError(f'{name}: expected a single Codex invocation policy in agents/openai.yaml')
    if re.search(r'^dependencies\s*:', value, re.M):
        raise ValueError(f'{name}: external tool dependencies require explicit review and validator support')
    return match[1] == 'false'


def git_text(checkout, *args):
    result = subprocess.run(['git', '-C', str(checkout), *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode('utf-8', errors='replace').strip() or 'Could not read toolkit Git object')
    return normalize(result.stdout)


def toolkit_blob(checkout, commit, path):
    entry = git_text(checkout, 'ls-tree', commit, '--', path)
    if not entry.startswith(('100644 blob ', '100755 blob ')):
        raise ValueError(f'Toolkit file is missing or not a regular file: {path}')
    return git_text(checkout, 'show', f'{commit}:{path}')


def change_plan(root, toolkit):
    changes = {}

    def read(relative):
        path = path_at(root, relative)
        return normalize(path.read_bytes()) if path.exists() else None

    def plan(relative, value):
        current = read(relative)
        if current != value:
            changes[relative] = (current, value)

    lock = json.loads(read(LOCK) or '{}')
    if set(lock) != {'toolkit_commit', 'shared', 'local', 'hashes'}:
        raise ValueError(f'{LOCK}: expected toolkit_commit, shared, local and hashes')
    shared, local, hashes = lock['shared'], lock['local'], lock['hashes']
    if not isinstance(shared, list) or not isinstance(local, list):
        raise ValueError('shared and local must be lists of skill names')
    names = shared + local
    if (any(not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name) for name in names)
            or len(set(names)) != len(names)):
        raise ValueError('Skill names must be unique lowercase kebab-case')

    sources = SHARED_FILES.copy()
    for name in shared:
        for filename in ('SKILL.md', 'agents/openai.yaml'):
            sources[f'.agents/skills/{name}/{filename}'] = f'skills/{name}/{filename}'
    pin = lock['toolkit_commit']
    uninitialized = pin is None and hashes == {}
    if not uninitialized and (not isinstance(pin, str) or not SHA.fullmatch(pin) or not isinstance(hashes, dict)
            or hashes.keys() != sources.keys() or any(not isinstance(h, str) or not DIGEST.fullmatch(h) for h in hashes.values())):
        raise ValueError(f'{LOCK}: invalid toolkit commit or hashes')
    if uninitialized and toolkit is None:
        raise ValueError('Initialize the lock with --sync --toolkit PATH')

    instructions = read('AGENTS.md')
    if instructions is None or not instructions.strip():
        raise ValueError('Missing AGENTS.md')
    if read('AGENTS.override.md') is not None:
        raise ValueError('Root AGENTS.override.md would mask the standards in AGENTS.md')
    block = re.search(rf'^{re.escape(BEGIN)}\n(.*?)^{re.escape(END)}$', instructions, re.M | re.S)
    if instructions.count(BEGIN) != bool(block) or instructions.count(END) != bool(block):
        raise ValueError('Invalid standards markers in AGENTS.md')
    installed = {key: read(key) for key in sources if key != '@standards'}
    installed['@standards'] = block[1] if block else None

    if toolkit is None:
        for key, current in installed.items():
            if current is None or digest(current) != hashes[key]:
                raise ValueError(f'Shared drift or missing file: {key}. Restore the pinned file or sync from the pinned toolkit commit.')
    else:
        commit = git_text(toolkit, 'rev-parse', '--verify', 'HEAD^{commit}').strip()
        updated = {key: toolkit_blob(toolkit, commit, source) for key, source in sources.items()}
        for key, value in updated.items():
            current = installed[key]
            if key == '@standards' and (not value.endswith('\n') or BEGIN in value or END in value or ABSOLUTE.search(value)):
                raise ValueError('Invalid toolkit standards')
            if current is not None and current != value and (key not in hashes or digest(current) != hashes[key]):
                raise ValueError(f'Would overwrite unrecognized content: {key}')
            if key != '@standards':
                plan(key, value)
        standards = f'{BEGIN}\n{updated["@standards"]}{END}'
        if block:
            instructions = instructions[:block.start()] + standards + instructions[block.end():]
        else:
            heading_pattern = re.compile(r'^# .+$', re.M)
            offset = 0
            if instructions.startswith('<!-- BEGIN:nextjs-agent-rules -->'):
                offset = instructions.index('<!-- END:nextjs-agent-rules -->') + len('<!-- END:nextjs-agent-rules -->')
            heading = heading_pattern.search(instructions, offset)
            offset = heading.end() if heading else offset
            separator = '\n\n' if offset else ''
            instructions = instructions[:offset] + separator + standards + '\n\n' + instructions[offset:].lstrip('\n')
        plan('AGENTS.md', instructions)
        installed = updated
        lock.update(toolkit_commit=commit, hashes={key: digest(value) for key, value in updated.items()})

    claude = read('CLAUDE.md')
    if claude is None:
        plan('CLAUDE.md', '@AGENTS.md\n')
    elif '@AGENTS.md' not in claude.splitlines():
        raise ValueError('CLAUDE.md must import @AGENTS.md on its own line')

    for name in sorted(names):
        source = f'.agents/skills/{name}/SKILL.md'
        metadata = f'.agents/skills/{name}/agents/openai.yaml'
        target = f'.claude/skills/{name}/SKILL.md'
        value = installed.get(source) if name in shared else read(source)
        if value is None:
            raise ValueError(f'Missing skill: {source}')
        header, body = parse_skill(value, name)
        explicit = manual_only(installed.get(metadata) if name in shared else read(metadata), name)
        # shortcut: only repository docs and web links are used, add bundled references when a workflow needs them
        links = re.findall(r'\]\(([^\s)]+)\)', body)
        documents = re.findall(r'(?<![\w./-])docs/[\w./-]+\.md\b', body)
        for link in links:
            if link.startswith(('https://', 'http://', '#')):
                continue
            path = link.split('#', 1)[0]
            if path.startswith('../../../docs/'):
                documents.append(path.removeprefix('../../../'))
            else:
                raise ValueError(f'{name}: unsupported skill link: {link}')
        for dependency in set(documents):
            if not path_at(root, dependency).is_file():
                raise ValueError(f'{name}: missing dependency {dependency}')

        marker = f'<!-- Generated by scripts/ai-skills.py from {source}. -->'
        if read(f'.claude/commands/{name}.md') is not None:
            raise ValueError(f'{name}: duplicate Claude command and skill name')
        current = read(target)
        if current is not None and marker not in current.splitlines()[:7]:
            raise ValueError(f'Would overwrite an unmanaged Claude skill: {target}')
        policy = '\ndisable-model-invocation: true' if explicit else ''
        plan(target, f'---\n{header}{policy}\n---\n\n{marker}\n{body}')

    for directory in ('.agents/skills', '.claude/skills'):
        for skill in (root / directory).glob('*/SKILL.md'):
            relative = skill.relative_to(root).as_posix()
            value = read(relative)
            if skill.parent.name not in names and (directory == '.agents/skills' or '<!-- Generated by scripts/ai-skills.py from ' in value):
                raise ValueError(f'Unlisted skill: {relative}. Update the lock and review any obsolete generated files.')

    if toolkit is not None:
        plan(LOCK, json.dumps(lock, indent=2, sort_keys=True) + '\n')
    return changes


def apply(root, changes):
    for relative, (expected, value) in changes.items():
        path = path_at(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if expected is None:
            with path.open('xb') as target:
                target.write(value.encode('utf-8'))
            continue
        raw = path.read_bytes()
        if normalize(raw) != expected:
            raise ValueError(f'File changed during sync: {relative}. Review it before retrying.')
        if b'\r\n' in raw:
            value = value.replace('\n', '\r\n')
        with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(value.encode('utf-8'))
        try:
            os.replace(temporary.name, path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='validate the committed lock and generated files offline (default)')
    mode.add_argument('--sync', action='store_true', help='regenerate Claude skills and optionally upgrade the toolkit pin')
    parser.add_argument('--dry-run', action='store_true', help='show the sync changes without writing files')
    parser.add_argument('--toolkit', type=Path, help='take shared files from HEAD of this local toolkit checkout')
    args = parser.parse_args()
    syncing = args.sync or args.dry_run
    if (args.check and syncing) or (args.toolkit is not None and not syncing):
        parser.error('--toolkit and --dry-run apply to sync only')
    root = Path(__file__).resolve().parents[1]
    try:
        changes = change_plan(root, args.toolkit)
        for path in changes:
            print(f'{"Would update" if args.dry_run else "Update" if syncing else "Out of sync"}: {path}')
        if args.sync and not args.dry_run:
            apply(root, changes)
        if not changes:
            print('AI skills and instructions are in sync.')
        return int(bool(changes) and not syncing)
    except (ValueError, OSError) as error:
        print(f'AI skills: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
