import os, socket, subprocess, time, urllib.request, json
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[2]
with socket.socket() as s:
    s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
env = os.environ.copy()
env['BEECODE_WORKDIR'] = rf'C:\tmp\beecode-project-test-{port}'
env['BEECODE_TEST_WEBVIEW_DIR'] = rf'C:\tmp\beecode-rust-test-webview-{port}'
env['BEECODE_TEST_CDP_PORT'] = str(port)
env.pop('BEECODE_REPO', None)
proc = subprocess.Popen([str(root / 'rust-shell/target/release/beecode-rust-test.exe')], cwd=str(root), env=env)
try:
    endpoint = f'http://127.0.0.1:{port}'
    for i in range(120):
        try:
            with urllib.request.urlopen(endpoint + '/json/version', timeout=1) as response:
                version = json.load(response)
            break
        except Exception:
            if proc.poll() is not None: raise AssertionError('Rust window exited early')
            time.sleep(.5)
    else: raise AssertionError('WebView debugger did not start')
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(endpoint)
        page = next(page for context in browser.contexts for page in context.pages if '/app/' in page.url)
        page.wait_for_function("window.beeDesk && document.querySelector('.rust-window-controls')", timeout=30000)
        page.add_style_tag(content="#gate { display: none !important; }")
        from urllib.parse import urlsplit
        backend_port = urlsplit(page.url).port
        assert page.locator('.rust-window-controls button').count() == 3
        assert page.locator('.rust-resize').count() == 8
        page.locator('#nav-sidebar').click()
        page.wait_for_timeout(400)
        assert not page.locator('#sidebar').is_visible()
        page.locator('#nav-sidebar').click()
        page.wait_for_timeout(400)
        assert page.locator('#sidebar').is_visible()
        result = page.evaluate("""async () => Promise.race([window.beeDesk.openExternal('file:///C:/invalid'), new Promise(r => setTimeout(() => r('timeout'), 3000))])""")
        assert result is False, f'IPC did not reject unsafe URL: {result}'
        result = page.evaluate("""async () => Promise.race([window.beeDesk.menuAction('zoomIn'), new Promise(r => setTimeout(() => r('timeout'), 3000))])""")
        assert result is True, f'IPC zoom failed: {result}'
        assert page.evaluate('document.body.style.zoom') != ''
        page.locator('#project-create').click()
        page.locator('#project-name').fill('Тестовый проект')
        page.locator('#project-form button[type=submit]').click()
        page.get_by_role('button', name='Проект: Тестовый проект').wait_for(timeout=10000)
        page.locator('.project-line').first.get_by_role('button', name='Создать папку').click()
        page.locator('#project-name').fill('Тестовая папка')
        page.locator('#project-form button[type=submit]').click()
        folder = page.locator('.project-line').filter(has_text='Тестовая папка')
        folder.wait_for(timeout=10000)
        folder.get_by_role('button', name='Создать чат').click()
        page.wait_for_function("() => projectsCache.some(p => p.chats?.length > 0)", timeout=10000)
        with urllib.request.urlopen(f'http://127.0.0.1:{backend_port}/api/projects') as response:
            projects = json.load(response)['projects']
        assert len(projects) == 1 and len(projects[0]['chats']) == 1
        assert projects[0]['chats'][0]['folder'] == projects[0]['folders'][0]['id']
        page.locator('.rust-window-controls button').last.click()
        proc.wait(timeout=15)
        for _ in range(30):
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', backend_port)) != 0: break
            time.sleep(.1)
        else: raise AssertionError('Agent server remains alive after window closes')
        print('RUST_SMOKE_OK: WebView, sidebar, restricted IPC, zoom, close')
finally:
    if proc.poll() is None:
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        proc.wait(timeout=10)
