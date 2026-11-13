"""Built-in, local stdio MCP server for BeeCode customization."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from beeagent.tools.ui import UiTool

SERVER = 'customuze_beecode'

def instructions_path(workdir):
    key = hashlib.sha256(os.path.normcase(str(Path(workdir).resolve())).encode('utf-8')).hexdigest()
    return Path.home() / '.beecode' / 'customize' / (key + '.json')

def read_instructions(workdir):
    try:
        path = instructions_path(workdir)
        if path.is_symlink() or path.stat().st_size > 65536:
            return ''
        obj = json.loads(path.read_text(encoding='utf-8'))
        return str(obj.get('instructions', ''))[:12000] if isinstance(obj, dict) else ''
    except (OSError, ValueError, UnicodeError):
        return ''

def write_instructions(workdir, value):
    path = instructions_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Invalid settings path')
    fd, tmp = tempfile.mkstemp(prefix='instructions-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'instructions': value}, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

TOOLS = [
    {'name':'ui', 'description':UiTool.description, 'inputSchema':UiTool.parameters},
    {'name':'instructions', 'description':'Read or change additional user system instructions for this workspace. Built-in BeeCode rules stay intact.',
     'inputSchema':{'type':'object','properties':{'action':{'type':'string','enum':['get','set','clear']},
        'text':{'type':'string','description':'Complete replacement instructions, up to 12000 characters'}},'required':['action']}}
]

def call(name, args, workdir):
    if not isinstance(args, dict): raise ValueError('arguments must be an object')
    if name == 'ui':
        result = UiTool().execute(**args)
        return result.output, result.error
    if name != 'instructions': raise ValueError('Unknown tool')
    action = args.get('action')
    if action == 'get': return read_instructions(workdir) or '(no additional instructions)', False
    if action not in ('set', 'clear'): raise ValueError('Use get, set or clear')
    value = args.get('text') if action == 'set' else ''
    if not isinstance(value, str) or len(value) > 12000 or (action == 'set' and not value.strip()):
        raise ValueError('Provide non-empty text of at most 12000 characters')
    write_instructions(workdir, value)
    return 'Additional user instructions updated; the next model request will use them.', False

def main():
    workdir = str(Path(sys.argv[1]).resolve()) if len(sys.argv) > 1 else os.getcwd()
    os.chdir(workdir)
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict): continue
            ident, method = message.get('id'), message.get('method')
            if ident is None: continue
            params = message.get('params') or {}
            if method == 'initialize':
                result = {'protocolVersion':params.get('protocolVersion','2025-03-26'), 'capabilities':{'tools':{}},
                          'serverInfo':{'name':'customuze beecode','version':'1.0.0'}}
            elif method == 'ping': result = {}
            elif method == 'tools/list': result = {'tools':TOOLS}
            elif method == 'tools/call':
                try:
                    output, error = call(params.get('name'), params.get('arguments', {}), workdir)
                    result = {'content':[{'type':'text','text':str(output)}], 'isError':bool(error)}
                except (ValueError, TypeError, OSError) as exc:
                    result = {'content':[{'type':'text','text':str(exc)}], 'isError':True}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result}, ensure_ascii=True), flush=True)
        except (ValueError, TypeError):
            continue

if __name__ == '__main__': main()
