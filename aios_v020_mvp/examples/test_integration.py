"""Final integration test - boots the server, exercises the API, shuts down."""
import os, sys, json, time, threading, subprocess, urllib.request, urllib.error, shutil, tempfile

sys.path.insert(0, '.')

tmp = tempfile.mkdtemp(prefix='aios_final_')
os.environ['AIOS_MVP_DATA_DIR'] = tmp
os.environ['AIOS_MVP_OFFLINE'] = '1'
os.environ['AIOS_MVP_PORT'] = '18995'

# Spawn the server as a separate Python process
server_proc = subprocess.Popen(
    [sys.executable, '-m', 'aios_v020_mvp.server'],
    cwd='.',
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
print('spawned pid', server_proc.pid)

try:
    # Wait for server to come up
    for i in range(50):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18995/health', timeout=1) as resp:
                if resp.status == 200:
                    print('server up after', i * 0.1, 's')
                    break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError('server did not come up')

    # Health
    with urllib.request.urlopen('http://127.0.0.1:18995/health', timeout=5) as resp:
        health = json.loads(resp.read().decode())
        print('health ok=', health['ok'], 'service=', health['service'])

    # Submit + poll
    req = urllib.request.Request(
        'http://127.0.0.1:18995/task',
        data=json.dumps({'input': 'Write a file called integration.txt with hello world.'}).encode(),
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode())
        wid = body['task_id']

    # Poll
    for _ in range(60):
        time.sleep(0.2)
        with urllib.request.urlopen(f'http://127.0.0.1:18995/task/{wid}', timeout=5) as resp:
            doc = json.loads(resp.read().decode())
            if doc['workflow']['stage'] in ('completed', 'failed'):
                break

    print('final stage:', doc['workflow']['stage'])
    print('verdict:', doc['workflow']['review']['verdict'])
    print('artefact:', doc['workflow']['execution']['artefacts'][0]['path'])

    # List artefacts
    with urllib.request.urlopen(f'http://127.0.0.1:18995/task/{wid}/artefacts', timeout=5) as resp:
        listing = json.loads(resp.read().decode())
        print('artefacts listed:', listing['count'])

    # Read artefact
    with urllib.request.urlopen(f'http://127.0.0.1:18995/task/{wid}/artefacts/integration.txt', timeout=5) as resp:
        content = json.loads(resp.read().decode())
        print('content first line:', content['content'].splitlines()[0])

    print()
    print('INTEGRATION TEST PASSED')
finally:
    server_proc.terminate()
    try:
        server_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server_proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)
