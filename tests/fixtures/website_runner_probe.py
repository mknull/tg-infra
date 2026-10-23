import json, os, shutil, subprocess, tempfile, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

with tempfile.TemporaryDirectory(prefix='website-runner-probe-') as tmp:
    root = Path(tmp)
    work = root / 'checkout'
    work.mkdir()
    sentinel = work / 'forbidden.txt'
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if '/count_tokens' in self.path:
                body = b'{"input_tokens":1}'
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
            observed.append(value)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            def event(name, data):
                self.wfile.write(('event: ' + name + '\ndata: ' + json.dumps(data) + '\n\n').encode())
                self.wfile.flush()
            event('message_start', {'type':'message_start','message':{'id':'msg_test','type':'message','role':'assistant','content':[],'model':value.get('model','fixture'),'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':0}}})
            if len(observed) == 1:
                event('content_block_start', {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'forbidden_call','name':'Write','input':{}}})
                event('content_block_delta', {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps({'file_path':str(sentinel),'content':'must not be written'})}})
                stop = 'tool_use'
            else:
                event('content_block_start', {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})
                event('content_block_delta', {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'{"result":"blocked"}'}})
                stop = 'end_turn'
            event('content_block_stop', {'type':'content_block_stop','index':0})
            event('message_delta', {'type':'message_delta','delta':{'stop_reason':stop,'stop_sequence':None},'usage':{'output_tokens':12}})
            event('message_stop', {'type':'message_stop'})
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    env = {k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL','TZ')}
    env.update(ANTHROPIC_API_KEY='fixture-not-a-real-key', ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_port}', CLAUDE_CONFIG_DIR=str(root/'config'), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1')
    allowed = 'Glob,Grep,Read,WebFetch,WebSearch'
    args = [shutil.which('claude'),'-p','--restricted','--tools',allowed,'--allowedTools',allowed,'--permission-mode','dontAsk','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--setting-sources','','--settings','{"disableAllHooks":true}','--disable-slash-commands','--no-session-persistence','--output-format','stream-json','--verbose','--max-budget-usd','1','--system-prompt','Testing read-only tool enforcement with a local fixture.']
    try:
        result = subprocess.run(args,input='Return a result.',text=True,capture_output=True,cwd=work,env=env,timeout=45)
        print('exit:', result.returncode, 'model_requests:', len(observed), 'forbidden_file_exists:',sentinel.exists())
        print('offered_tools:', [tool['name'] for tool in observed[0].get('tools',[])] if observed else [])
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        attempted = [block['name'] for event in events for block in event.get('message',{}).get('content',[]) if isinstance(block,dict) and block.get('type')=='tool_use']
        print('recorded_tool_attempts:', attempted)
        print('stderr:',result.stderr[-1500:])
        assert observed and not sentinel.exists() and 'Write' in attempted
        assert 'Write' not in [tool['name'] for tool in observed[0].get('tools',[])]
    finally:
        server.shutdown(); server.server_close()
