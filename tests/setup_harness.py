"""Process fixtures: replace provider transports, never wizard/verification logic."""
import json
import os
import pty
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

PROVIDERS = r'''
import io, json, os, sqlite3, urllib.request
from pathlib import Path

def record(kind, **values):
    with open(os.environ['SETUP_PROVIDER_LOG'], 'a') as stream:
        stream.write(json.dumps({'kind': kind, **values}) + '\n')
class Response(io.BytesIO):
    status = 200
    headers = {'Content-Type': 'application/xml'}
    def __init__(self, value, url):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.url = url
    def geturl(self): return self.url
    def getcode(self): return self.status

def model(prompt):
    record('model')
    if Path(os.environ['SETUP_PROVIDER_LOG'] + '.invalid').exists(): return 'invalid JSON'
    if 'List every fact' in prompt: return 'Alex is an accessibility designer.'
    if 'career analyst' in prompt:
        return '\n'.join('###FILE: ' + n + '###\nAlex is an accessibility designer.' for n in ('interests.txt', 'skills.txt', 'tech_stack.txt'))
    if 'Select RSS job feeds' in prompt: return '{"choices":[{"id":"wwr:design","reason":"Design roles"}]}'
    if 'Generate Flash job-filter' in prompt:
        if Path(os.environ['SETUP_PROVIDER_LOG'] + '.invalid-flash').exists(): return 'invalid JSON'
        sources = json.loads(prompt.split('SOURCES:\n',1)[1].split('\n\nReturn ONLY JSON:',1)[0])
        return json.dumps({'sources':[{'id':s['id'],'desired_roles':'Accessibility designer','acceptable_roles':'User researcher'} for s in sources]})
    if 'You are a job posting filter' in prompt: return '{"decision":"pass_to_pro","reason":"Designer"}'
    if 'role_type' in prompt: return '{"decision":"send","reason":"Relevant","message":"Design role","tags":{"role_type":"design"}}'
    return '# Candidate Profile\nAlex is an accessibility designer. Send relevant design roles.'

def urlopen(request, *args, **kwargs):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    payload = json.loads(request.data) if isinstance(request, urllib.request.Request) and request.data and request.headers.get('Content-type') == 'application/json' else {}
    if url.startswith('https://api.deepseek.com/'):
        return Response({'choices':[{'message':{'content':model(payload['messages'][0]['content'])}}]}, url)
    if url.startswith('https://api.telegram.org/'):
        method = url.rsplit('/',1)[-1]; record('bot', method=method)
        value = {'ok':True, 'result':{'username':'fixturebot','id':123,'first_name':'Fixture'}}
        if method == 'getUpdates': value['result'] = [{'message':{'chat':{'id':123,'type':'private'}}}]
        if method == 'sendMessage':
            if Path(os.environ['SETUP_PROVIDER_LOG'] + '.delivery-fail').exists(): return Response({'ok':False,'description':'fixture failure'},url)
            value['result'] = {'message_id':42}
        return Response(value,url)
    if url.startswith('https://login.microsoftonline.com/'):
        record('outlook_auth')
        if url.endswith('/devicecode'): return Response({'verification_uri':'https://example.test/device','user_code':'fixture','expires_in':60,'interval':0,'device_code':'fixture'},url)
        return Response({'access_token':'fixture','refresh_token':'fixture','expires_in':3600},url)
    if url.startswith('https://graph.microsoft.com/'):
        record('graph', path=url.split('/v1.0/',1)[-1].split('?',1)[0])
        return Response({'value':[{'id':'jobs-folder'}]},url)
    if url.startswith('https://weworkremotely.com/'):
        record('feed')
        if Path(os.environ['SETUP_PROVIDER_LOG'] + '.feed-fail').exists(): raise OSError('fixture feed outage')
        return Response(b'<rss version="2.0"><channel><title>Design jobs</title><link>https://example.test/</link><item><guid>design1</guid><title>Accessibility designer</title><link>https://example.test/design1</link><description>Paid design and research role. Work on accessible products. Full description of responsibilities and required qualifications for the design team.</description></item></channel></rss>',url)
    if url.startswith('http://localhost:8080/search'):
        record('search')
        return Response({'results':[{'title':'Fixture jobs','url':'https://example.test/jobs','content':'Jobs'}]},url)
    raise AssertionError('Unexpected provider request: ' + url)
urllib.request.urlopen = urlopen
try:
    import telethon
    class Client:
        def __init__(self, session, *args, **kwargs): self.session=Path(session+'.session')
        async def start(self):
            record('telegram_read')
            if not self.session.exists():
                record('telegram_auth')
                with sqlite3.connect(self.session) as db: db.execute('create table fixture (value text)')
            return self
        async def get_messages(self, username, limit=1): return []
        async def disconnect(self): pass
    telethon.TelegramClient=Client
except ImportError: pass
try:
    import playwright.sync_api
    class Browser:
        def close(self): pass
    class Playwright:
        def __enter__(self): self.chromium=self; return self
        def __exit__(self,*args): pass
        def launch(self,**kwargs): record('browser'); return Browser()
    playwright.sync_api.sync_playwright=Playwright
except ImportError: pass
'''

SYSTEMCTL = r'''
import json, os, sys
from pathlib import Path
path=Path(os.environ['SETUP_FAKE_SYSTEMD']); state=json.loads(path.read_text()) if path.exists() else {'enabled':[], 'active':[], 'calls':[]}
args=sys.argv[1:]; args.remove('--user')
command=args[0]; unit=args[1] if len(args)>1 else ''
state['calls'].append(args)
path.write_text(json.dumps(state))
if command=='show-environment': sys.exit(0)
if command in ('is-active','is-enabled'):
    present=unit in state['active' if command=='is-active' else 'enabled']
    print(('active' if command=='is-active' else 'enabled') if present else 'inactive')
    sys.exit(0 if present else 3)
if command=='enable' and Path(str(path)+'.fail').exists() and unit.endswith('rss-poll.timer'):
    print('fixture activation failure',file=sys.stderr); sys.exit(1)
if command in ('enable','disable','start','stop'):
    key='enabled' if command in ('enable','disable') else 'active'
    state[key]=[u for u in state[key] if u!=unit]
    if command in ('enable','start'): state[key].append(unit)
path.write_text(json.dumps(state))
'''


class InstallHarness:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='guided-install-')
        self.path = Path(self.temporary.name)
        self.code = self.path / 'project'
        self.code.mkdir()
        for src in PROJECT.glob('*.py'):
            shutil.copy2(src, self.code / src.name)
        for name in ('setup.sh', 'audit', 'email-ingest-wrap', 'requirements.txt', 'channels.json.example',
                     'feeds.json.example','it-jobs-criteria.md.example','email-triage-criteria.md.example'):
            shutil.copy2(PROJECT / name, self.code / name)
        shutil.copytree(PROJECT / 'lib', self.code / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
        (self.code / 'sitecustomize.py').write_text(PROVIDERS)
        self.data = self.path / 'installation'
        self.data.mkdir()
        self.commands = self.path / 'commands'; self.commands.mkdir()
        ctl = self.commands / 'systemctl'
        ctl.write_text('#!' + sys.executable + ' -S\n' + SYSTEMCTL); ctl.chmod(0o755)
        docker = self.commands / 'docker'
        docker.write_text('#!/bin/sh\nexit 0\n'); docker.chmod(0o755)
        python = self.code / 'jobsmcp/bin/python3'; python.parent.mkdir(parents=True)
        python.write_text('#!/bin/bash\nif [[ "$1" == "-m" && ( "$2" == "pip" || "$2" == "playwright" ) ]]; then exit 0; fi\nexec ' + shlex.quote(sys.executable) + ' "$@"\n'); python.chmod(0o755)
        self.provider_log = self.path / 'providers.jsonl'
        self.systemd_log = self.path / 'systemd.json'
        self.env = {**os.environ, 'TG_INFRA_DATA_DIR':str(self.data), 'PYTHONPATH':str(self.code),
                    'XDG_CONFIG_HOME':str(self.path/'config'), 'PATH':str(self.commands)+os.pathsep+os.environ['PATH'],
                    'SETUP_PROVIDER_LOG':str(self.provider_log), 'SETUP_FAKE_SYSTEMD':str(self.systemd_log)}

    def close(self): self.temporary.cleanup()

    def calls(self):
        return [json.loads(line) for line in self.provider_log.read_text().splitlines()] if self.provider_log.exists() else []

    def drive(self, answers, *, timeout=50):
        master, slave = pty.openpty()
        proc = subprocess.Popen(['bash',str(self.code/'setup.sh')], cwd=self.code, env=self.env,
                                stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
        os.close(slave)
        text=''; position=0; deadline=time.monotonic()+timeout
        def read():
            nonlocal text
            if select.select([master],[],[],0.1)[0]:
                try: text += os.read(master,65536).decode(errors='replace')
                except OSError: pass
        try:
            for needle, answer in answers:
                while needle not in text[position:]:
                    read()
                    if proc.poll() is not None or time.monotonic()>deadline:
                        raise AssertionError('Did not reach '+repr(needle)+'\n'+text)
                position=text.index(needle,position)+len(needle)
                os.write(master,(answer+'\n').encode())
            while proc.poll() is None and time.monotonic()<deadline: read()
            if proc.poll() is None: raise AssertionError('Setup did not finish\n'+text)
            for _ in range(3): read()
            return proc.returncode,text
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            os.close(master)

    def fresh_answers(self, *, rss=True, telegram=False, email=False, weekly=False, briefings=False, canary=False, action='l'):
        answers=[('RSS/Atom job feeds?', 'y' if rss else 'n'),('Telegram groups as an INPUT', 'y' if telegram else 'n'),
                 ('Outlook job-email input?', 'y' if email else 'n'),('Researched /briefme reports', 'y' if briefings else 'n'),
                 ('Weekly reports and email feedback', 'y' if weekly else 'n'),('Hourly Telegram delivery', 'y' if canary else 'n'),
                 ('TELEGRAM_BOT_TOKEN:', '123:fixture'),('DEEPSEEK_API_KEY:', 'fixture-model-key')]
        if telegram: answers += [('TELEGRAM_API_ID:', '123'),('TELEGRAM_API_HASH:', 'fixture-hash')]
        if email or weekly: answers += [('OUTLOOK_CLIENT_ID:', 'fixture-client'),('OUTLOOK_EMAIL:', 'alex@example.test')]
        if email: answers += [('OUTLOOK_FOLDER:', 'Job alerts')]
        answers += [('USER_NAME:', 'Alex')]
        if telegram: answers += [('Telegram usernames,', 'design_jobs'),('What does @design_jobs publish?', 'Design jobs'),('Typical language,', 'English full descriptions')]
        answers += [('Generate job-matching instructions', 'y'),('Documents directory', ':text'),('Finish with a line containing only a dot.', 'Alex designs accessible products.\n.'),
                    ('Additional preferences', ''),('Generate this proposal now?', 'y')]
        if rss: answers += [('RSS feeds:', 'r'),('[R]ecent 30 days', 'a')]
        answers += [('[A]pply / [R]evise preferences', 'a'),('[S]tart / [C]ustomize',action)]
        if action=='s': answers += [("Press Enter once you've sent it", '')]
        return answers
