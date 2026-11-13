"""Search skills.sh and install a selected GitHub skill into BeeCode."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from urllib.parse import quote

import httpx
from beeagent.tools.base import BaseTool, ToolResult

SEARCH_URL = 'https://skills.sh/api/search'
GITHUB_API = 'https://api.github.com'
RAW = 'https://raw.githubusercontent.com'
IDENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
SHA = re.compile(r'^[a-f0-9]{40}$')
MAX_FILE = 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024
MAX_FILES = 120


def _json(url, params=None):
    response = httpx.get(url, params=params, headers={'User-Agent':'BeeCode-skill-finder',
                         'Accept':'application/vnd.github+json'}, timeout=20, follow_redirects=False)
    response.raise_for_status()
    if len(response.content) > 16 * 1024 * 1024: raise ValueError('Response is too large')
    return response.json()


def _bytes(url, limit=MAX_FILE):
    with httpx.stream('GET', url, headers={'User-Agent':'BeeCode-skill-finder'},
                      timeout=20, follow_redirects=False) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > limit: raise ValueError('Skill file is too large')
        return bytes(data)


def _name(source, skill):
    parts = str(source or '').split('/')
    if len(parts) != 2 or not all(IDENT.fullmatch(p) and p not in ('.','..') for p in parts):
        raise ValueError('Source must be a GitHub owner/repository')
    if not IDENT.fullmatch(str(skill or '')) or skill in ('.','..'):
        raise ValueError('Invalid skill name')
    return parts[0], parts[1], skill


def _safe_relative(path):
    parts = PurePosixPath(path).parts
    if not parts or len(parts) > 20 or any(p in ('', '.', '..') or ':' in p or '\\' in p or
            p.endswith(('.', ' ')) or any(ord(c) < 32 for c in p) or
            re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p, re.I) for p in parts):
        raise ValueError('Unsafe path in skill')
    if len(parts) == 1 and parts[0].casefold() == 'plugin.py':
        raise ValueError('A skill cannot install an executable BeeCode plugin')
    return parts


def _blob(owner, repo, commit, entry):
    path = entry['path']
    url = f'{RAW}/{owner}/{repo}/{commit}/' + quote(path, safe='/')
    data = _bytes(url)
    expected = str(entry.get('sha') or '')
    if not SHA.fullmatch(expected): raise ValueError('Invalid GitHub file hash')
    actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if actual != expected: raise ValueError('Downloaded skill file does not match GitHub')
    return data


def _skill_name(raw):
    text = raw.decode('utf-8-sig')
    if not text.startswith('---\n') and not text.startswith('---\r\n'): return ''
    head = text.split('\n---', 1)[0]
    match = re.search(r'^name:\s*["\']?([^\s"\']+)', head, re.M)
    return match.group(1) if match else ''


class SkillFinderTool(BaseTool):
    name = 'skill_finder'
    description = 'Search skills.sh and install a selected GitHub skill in BeeCode. Use search, then install with source and skill.'
    parameters = {'type':'object','properties':{
        'action':{'type':'string','enum':['search','install']},
        'query':{'type':'string','description':'Search words'},
        'source':{'type':'string','description':'GitHub owner/repository from search'},
        'skill':{'type':'string','description':'Exact skill ID from search'}},'required':['action']}
    writes_files = True

    def __init__(self, agent): self.agent = agent
    def is_safe(self): return False  # Search sends a query; installation writes files.

    def execute(self, action, query='', source='', skill=''):
        try:
            if action == 'search': return self._search(query)
            if action == 'install': return self._install(source, skill)
            return ToolResult('Use action=search or action=install.', True)
        except (ValueError, OSError, httpx.HTTPError, UnicodeError, KeyError, TypeError) as exc:
            return ToolResult(f'skill-finder: {type(exc).__name__}: {str(exc)[:220]}', True)

    def _search(self, query):
        query = ' '.join(str(query or '').split())
        if not query or len(query) > 200: raise ValueError('Enter a search query up to 200 characters')
        data = _json(SEARCH_URL, {'q':query, 'limit':10})
        if not isinstance(data, dict) or not isinstance(data.get('skills'), list):
            raise ValueError('Invalid search response')
        found = []
        for item in data['skills'][:10]:
            if not isinstance(item, dict): continue
            source, skill = item.get('source'), item.get('skillId')
            try: _name(source, skill)
            except (ValueError, TypeError): continue
            found.append({'name':str(item.get('name') or skill)[:100], 'source':source, 'skill':skill,
                          'installs':item.get('installs') if isinstance(item.get('installs'), int) else 0,
                          'url':f'https://skills.sh/{source}/{skill}'})
        return ToolResult(json.dumps({'query':query,'results':found}, ensure_ascii=False), False)

    def _install(self, source, skill):
        owner, repo, skill = _name(source, skill)
        manager = self.agent.plugins.manager
        if skill == 'skill-finder': raise ValueError('This system skill is already built in')
        target = manager._plugin_dir(skill)
        if target.exists() or target.is_symlink() or manager.is_installed(skill):
            raise ValueError('A skill or plugin with this name already exists; it was not overwritten')
        commit_data = _json(f'{GITHUB_API}/repos/{owner}/{repo}/commits/HEAD')
        commit = str(commit_data.get('sha') or '')
        if not SHA.fullmatch(commit): raise ValueError('Invalid repository commit')
        tree = _json(f'{GITHUB_API}/repos/{owner}/{repo}/git/trees/{commit}', {'recursive':'1'})
        if tree.get('truncated') or not isinstance(tree.get('tree'), list):
            raise ValueError('Repository tree is incomplete')
        entries = tree['tree']
        candidates = [e for e in entries if isinstance(e, dict) and e.get('type') == 'blob' and
                      (e.get('path') == 'SKILL.md' or str(e.get('path','')).endswith('/SKILL.md'))]
        if len(candidates) > 60: raise ValueError('Too many skills in this repository to resolve safely')
        matches = []
        for item in candidates:
            if item.get('mode') not in ('100644', '100755'):
                continue
            data = _blob(owner, repo, commit, item)
            if _skill_name(data) == skill: matches.append((item, data))
        if len(matches) != 1: raise ValueError('Skill name is missing or ambiguous in this repository')
        selected, first = matches[0]
        prefix = selected['path'][:-len('SKILL.md')]
        files = [e for e in entries if isinstance(e, dict) and e.get('type') == 'blob' and
                 str(e.get('path','')).startswith(prefix)]
        if len(files) > MAX_FILES: raise ValueError('Skill contains too many files')
        root = Path(manager.plugins_dir)
        resolved = root.resolve()
        workspace = Path(self.agent.workdir).resolve()
        if (root.is_symlink() or root.parent.is_symlink() or
                resolved != workspace / '.beeagent' / 'plugins'):
            raise ValueError('Unexpected skills directory')
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.skill-finder-', dir=resolved))
        moved = False
        try:
            total = 0
            for entry in files:
                if entry.get('mode') not in ('100644', '100755'):
                    raise ValueError('Skill contains a symbolic link or unsupported file')
                relative = str(entry['path'])[len(prefix):]
                parts = _safe_relative(relative)
                raw = first if entry['path'] == selected['path'] else _blob(owner, repo, commit, entry)
                total += len(raw)
                if total > MAX_TOTAL: raise ValueError('Skill is too large')
                destination = staging.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                if os.name == 'posix' and entry.get('mode') == '100755':
                    destination.chmod(0o755)
            if target.exists() or target.is_symlink(): raise ValueError('Destination already exists')
            os.rename(staging, target); moved = True
            manager.install_local(skill, 'skill', description=f'From {owner}/{repo}')
            state = manager._state()
            state['installed'][skill]['source'] = {'kind':'github-skill', 'repo':f'{owner}/{repo}', 'commit':commit, 'path':selected['path']}
            manager._save_state(state)
            errors = self.agent.reload_extensions()
            if errors: return ToolResult('Skill installed, but reload reported: ' + '; '.join(errors), True)
            return ToolResult(f'Installed {skill} from https://github.com/{owner}/{repo} at {commit[:12]}. Available from the next model request.', False)
        finally:
            if not moved and staging.exists(): shutil.rmtree(staging)
