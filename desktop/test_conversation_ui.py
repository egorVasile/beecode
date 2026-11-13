"""Conversation rendering against an isolated, mocked desktop backend."""
import functools
import os
from pathlib import Path
import threading
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture()
def page():
    ui = Path(__file__).parent / "ui"
    server = ThreadingHTTPServer(("127.0.0.1", 0),
        functools.partial(SimpleHTTPRequestHandler, directory=str(ui)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with playwright.sync_playwright() as p:
        candidates = [os.environ.get("BEECODE_TEST_BROWSER", ""), p.chromium.executable_path,
                      r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                      r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
        executable = next((path for path in candidates if path and Path(path).is_file()), None)
        if not executable:
            server.shutdown()
            pytest.skip("No Chromium browser available")
        browser = p.chromium.launch(headless=True, executable_path=executable)
        context = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
        page = context.new_page()
        page.add_init_script("""window.fetch=async (url) => {
          const p=String(url); let d=p.includes('/api/health')?{ok:true,version:'test'}:
            p.includes('/api/whoami')?{name:'Tester'}:
            p.includes('/api/config')?{model:'test',provider:'test',permissions:'ask'}:
            p.includes('/api/models')?{models:[{id:'test'}]}:
            p.includes('/api/sessions')?{sessions:[]}:
            p.includes('/api/trust')?{trusted:true}:{};
          return new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}});
        }""")
        page.goto(f"http://127.0.0.1:{server.server_address[1]}/", wait_until="networkidle")
        page.evaluate("gateHide(); document.getElementById('greet').classList.add('hide')")
        yield page
        context.close()
        browser.close()
    server.shutdown()
    server.server_close()


def test_appearance_changes_and_survives_reload(page):
    page.locator('#gear').click()
    page.locator('[data-tab="appearance"]').click()
    page.locator('#appearance-theme [data-v="zinc"]').click()
    playwright.expect(page.locator('html')).to_have_attribute('data-theme', 'zinc')
    page.locator('#appearance-skin [data-v="paper"]').click()
    playwright.expect(page.locator('#skinlink')).to_have_attribute('href', 'skins/paper.css')
    page.reload(wait_until='networkidle')
    playwright.expect(page.locator('html')).to_have_attribute('data-theme', 'zinc')
    playwright.expect(page.locator('#skinlink')).to_have_attribute('href', 'skins/paper.css')
    page.evaluate("gateHide(); document.getElementById('greet').classList.add('hide')")
    page.locator('#gear').click()
    page.locator('[data-tab="appearance"]').click()
    playwright.expect(page.locator('#appearance-theme [data-v="zinc"]')).to_have_attribute('aria-pressed', 'true')
    playwright.expect(page.locator('#appearance-skin [data-v="paper"]')).to_have_attribute('aria-pressed', 'true')


def test_dark_settings_use_dark_edges_and_light_text(page):
    page.evaluate("applySkin('brutal'); applyTheme('zinc')")
    page.locator('#gear').click()
    page.locator('[data-tab="appearance"]').click()
    border = page.locator('#settings').evaluate("el => getComputedStyle(el).borderTopColor")
    text = page.locator('#settings .set-head b').evaluate("el => getComputedStyle(el).color")
    assert border == 'rgb(56, 56, 56)'
    assert text != border
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-dark-settings.png'), animations='disabled')
    page.locator('[data-tab="providers"]').click()
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-dark-providers.png'), animations='disabled')
    page.evaluate("applyTheme('paper')")
    assert page.locator('#settings').evaluate("el => getComputedStyle(el).borderTopColor") == 'rgb(22, 22, 22)'


def test_provider_form_sends_separate_model_metadata(page):
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = async (url, opts) => {
        if (String(url) === '/api/providers' && opts && opts.method === 'POST') {
          window.savedProvider = JSON.parse(opts.body);
          return new Response(JSON.stringify({ok:true,message:'Saved'}),
            {status:200,headers:{'Content-Type':'application/json'}});
        }
        return original(url, opts);
      };
    }""")
    page.locator('#gear').click()
    page.locator('#f-name').fill('test-provider')
    page.locator('#f-url').fill('https://example.com/v1')
    row = page.locator('.provider-model-row').first
    row.locator('[data-field="name"]').fill('Alpha')
    row.locator('[data-field="id"]').fill('org/alpha')
    row.locator('[data-field="context"]').fill('128000')
    row.locator('[data-field="reasoning"]').check()
    playwright.expect(row.locator('.model-efforts')).to_be_visible()
    playwright.expect(row.locator('.reasoning-toggle span')).to_be_hidden()
    row.locator('.model-efforts summary').click()
    row.locator('[data-effort="low"]').check()
    row.locator('[data-effort="high"]').check()
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-reasoning-levels.png'), animations='disabled')
    page.locator('#f-add-model').click()
    row2 = page.locator('.provider-model-row').nth(1)
    row2.locator('[data-field="id"]').fill('org/beta')
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-provider-models.png'), animations='disabled')
    page.locator('#f-save').click()
    page.wait_for_function('window.savedProvider !== undefined')
    body = page.evaluate('window.savedProvider')
    assert body['models'] == ['org/alpha', 'org/beta']
    assert body['modelDetails'][0] == {'id': 'org/alpha', 'name': 'Alpha', 'context': 128000, 'reasoning': True, 'efforts': ['low', 'high']}
    assert body['modelDetails'][1]['context'] is None
    page.evaluate('(details) => setProviderModelRows(details)', body['modelDetails'])
    restored = page.locator('.provider-model-row').first
    restored.locator('.model-efforts summary').click()
    playwright.expect(restored.locator('[data-effort="low"]')).to_be_checked()
    playwright.expect(restored.locator('[data-effort="high"]')).to_be_checked()
    restored.locator('[data-field="reasoning"]').uncheck()
    playwright.expect(restored.locator('.model-efforts')).to_be_hidden()


def test_model_rows_and_pool_buttons_follow_theme_and_viewport(page):
    page.evaluate("applySkin('brutal'); applyTheme('zinc')")
    page.locator('#gear').click()
    row = page.locator('.provider-model-row').first
    rects = row.locator(':scope > label').evaluate_all('(items) => items.map(x => x.getBoundingClientRect().top)')
    assert max(rects) - min(rects) < 28
    button = row.locator('[data-remove]')
    assert button.evaluate('el => getComputedStyle(el).backgroundColor') != 'rgb(239, 239, 239)'
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-model-settings-dark.png'), animations='disabled')
    page.locator('[data-tab="pool"]').click()
    for selector in ('#p-google', '#p-phone'):
        assert page.locator(selector).evaluate('el => getComputedStyle(el).backgroundColor') != 'rgb(239, 239, 239)'
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-pool-settings-dark.png'), animations='disabled')
    page.set_viewport_size({'width': 600, 'height': 850})
    page.locator('[data-tab="providers"]').click()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


def test_agent_ui_components_are_safe_reversible_and_survive_rerender(page):
    page.evaluate("""() => applyUiPrefs({version:1, updated_at:1,
      hiddenComponents:['new-chat','late-control'],
      components:[{id:'tip', kind:'notice', slot:'sidebar', text:'<img src=x onerror=alert(1)>'}]})""")
    playwright.expect(page.locator('#new-chat')).to_be_hidden()
    playwright.expect(page.locator('#bee-extra-tip')).to_have_text('<img src=x onerror=alert(1)>')
    assert page.locator('#bee-extra-tip img').count() == 0
    page.evaluate("""() => { const n=document.createElement('div'); n.id='late-control'; n.textContent='late'; document.body.append(n); }""")
    playwright.expect(page.locator('#late-control')).to_be_hidden()
    page.evaluate("applyUiPrefs({version:2, updated_at:2, hiddenComponents:[], components:[]})")
    playwright.expect(page.locator('#new-chat')).to_be_visible()
    playwright.expect(page.locator('#late-control')).to_be_visible()
    assert page.locator('#bee-extra-tip').count() == 0


def test_ui_shortcut_restores_hidden_interface(page):
    page.evaluate("""() => {
      const old = window.fetch;
      window.fetch = async (url, opts) => {
        if (url === '/api/ui/reset') return new Response(JSON.stringify({ok:true}), {status:200});
        if (url === '/api/ui') return new Response(JSON.stringify({version:0, hiddenComponents:[], components:[]}), {status:200});
        return old(url, opts);
      };
      applyUiPrefs({version:4, hiddenComponents:['sidebar'], components:[]});
    }""")
    playwright.expect(page.locator('#sidebar')).to_be_hidden()
    page.keyboard.press('Control+Shift+U')
    playwright.expect(page.locator('#sidebar')).to_be_visible()


def test_capture_custom_component_preview(page):
    page.evaluate("""() => {
      applyTheme('zinc');
      applyUiPrefs({version:7, updated_at:1, hiddenComponents:[], components:[
        {id:'note', kind:'notice', slot:'sidebar', text:'Задача: обновить интерфейс', button_action:''},
        {id:'settings', kind:'button', slot:'sidebar', text:'Открыть настройки', button_action:'settings'}]});
    }""")
    playwright.expect(page.locator('#bee-extra-note')).to_be_visible()
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-agent-components.png'), animations='disabled')
    page.locator('#bee-extra-settings').click()
    playwright.expect(page.locator('#settings-back')).to_be_visible()


def test_thinking_indicator_returns_after_a_tool_and_clears_for_answer(page):
    page.evaluate("""() => {
      window.testTurn = {box: assistantBox(), hadText: false};
      const event = (kind, data) => onFrame('event: '+kind+'\\ndata: '+JSON.stringify(data), window.testTurn);
      event('tool', {phase:'start', tool:'read', call:51});
      event('tool', {phase:'end', tool:'read', call:51, output:'file'});
    }""")
    playwright.expect(page.locator('#chat .thinking-indicator')).to_have_count(1)
    playwright.expect(page.locator('#chat .typing i')).to_have_count(3)
    page.evaluate("""() => onFrame('event: delta\\ndata: '+JSON.stringify({text:'Answer'}), window.testTurn)""")
    playwright.expect(page.locator('#chat .thinking-indicator')).to_have_count(0)
    playwright.expect(page.locator('#chat .assistant')).to_contain_text('Answer')


def test_all_tools_in_turn_share_a_collapsible_group(page):
    page.evaluate("""() => {
      window.turn = {box: assistantBox(), hadText:false};
      const event = (kind, data) => onFrame('event: '+kind+'\\ndata: '+JSON.stringify(data), window.turn);
      event('tool', {phase:'start',tool:'think',call:70});
      event('tool', {phase:'end',tool:'think',call:70,output:'ok'});
      event('tool', {phase:'start',tool:'read',call:71});
      event('tool', {phase:'end',tool:'read',call:71,output:'file'});
    }""")
    playwright.expect(page.locator('#chat .tool-group')).to_have_count(1)
    playwright.expect(page.locator('#chat .tool-group .tool')).to_have_count(2)
    page.locator('#chat .activity-summary').click()
    page.locator('#chat .tool').first.locator('summary').click()
    playwright.expect(page.locator('#chat .tool').first.locator('pre')).to_have_text('ok')
    page.evaluate("""() => {
      onFrame('event: delta\\ndata: '+JSON.stringify({text:'Answer'}), window.turn);
      onFrame('event: tool\\ndata: '+JSON.stringify({phase:'start',tool:'read',call:72}), window.turn);
    }""")
    playwright.expect(page.locator('#chat .tool-group')).to_have_count(1)


def test_adding_a_tool_updates_the_collapsed_group(page):
    page.emulate_media(reduced_motion='no-preference')
    animated = page.evaluate("""() => {
      toolRow('start', {tool:'think',call:90});
      toolRow('end', {tool:'think',call:90,output:'ok'});
      toolRow('start', {tool:'read',call:91});
      const group = document.querySelector('#chat .tool-group');
      return group.querySelector('.activity-count').textContent;
    }""")
    assert animated == '2'


def test_capture_conversation_preview(page, tmp_path):
    page.evaluate("""() => {
      userRow('Создай сайт');
      toolRow('start', {tool:'think', call:61});
      toolRow('end', {tool:'think', call:61, output:'План готов'});
      toolRow('start', {tool:'list_directory', call:62, args:{path:'src/'}});
      toolRow('end', {tool:'list_directory', call:62, output:'Файлы проекта'});
      assistantBox();
    }""")
    out = Path(tempfile.gettempdir())
    page.screenshot(path=str(out / 'preview-conversation-light.png'), animations='disabled')
    page.evaluate("applyTheme('zinc')")
    page.screenshot(path=str(out / 'preview-conversation-dark.png'), animations='disabled')


def test_reasoning_stream_is_not_mixed_into_answer(page):
    page.evaluate("""() => {
      const turn = {box: assistantBox(), hadText: false};
      onFrame('event: delta\\ndata: '+JSON.stringify({kind:'reasoning_delta',text:'private reasoning'}), turn);
      onFrame('event: delta\\ndata: '+JSON.stringify({kind:'stream_delta',text:'Final answer'}), turn);
      onFrame('event: done\\ndata: '+JSON.stringify({text:'Final answer'}), turn);
    }""")
    playwright.expect(page.locator('#chat .assistant')).to_contain_text('Final answer')
    playwright.expect(page.locator('#chat')).not_to_contain_text('private reasoning')


def test_done_without_stream_still_shows_answer(page):
    page.evaluate("""() => {
      const turn = {box: null, hadText: false};
      onFrame('event: done\\ndata: '+JSON.stringify({text:'Готовый ответ'}), turn);
    }""")
    playwright.expect(page.locator("#chat .assistant")).to_contain_text("Готовый ответ")


def test_tool_call_then_answer_and_error_details(page):
    page.evaluate("""() => {
      const turn = {box: assistantBox(), hadText: false};
      const event = (kind, data) => onFrame('event: '+kind+'\\ndata: '+JSON.stringify(data), turn);
      event('delta', {text:'{"tool":"read","args":{"path":"a.py"}}'});
      event('tool', {phase:'start',tool:'read',call:1,args:{path:'a.py'}});
      event('tool', {phase:'end',tool:'read',call:1,output:'строка'});
      event('delta', {text:'**Ответ после инструмента**'});
      event('done', {});
      toolRow('start', {tool:'bash',call:2,args:{command:'pytest'}});
      toolRow('end', {tool:'bash',call:2,error:true,output:'<script>bad()</script>'});
    }""")
    playwright.expect(page.locator("#chat .assistant strong")).to_have_text("Ответ после инструмента")
    page.locator('#chat .activity-summary').click()
    cards = page.locator("#chat .tool-group details.tool")
    playwright.expect(cards).to_have_count(2)
    playwright.expect(cards.nth(0)).not_to_have_attribute("open", "")
    playwright.expect(cards.nth(0)).to_contain_text("a.py")
    playwright.expect(cards.nth(1)).to_have_attribute("open", "")
    playwright.expect(cards.nth(1).locator("pre")).to_have_text("<script>bad()</script>")
    assert cards.nth(1).locator("script").count() == 0
    cards.nth(0).locator("summary").focus()
    page.keyboard.press("Enter")
    playwright.expect(cards.nth(0)).to_have_attribute("open", "")


def test_stream_formats_markdown_and_sanitizes_html(page):
    page.evaluate("""() => {
      const turn = {box: assistantBox(), hadText: false};
      onFrame('event: delta\\ndata: '+JSON.stringify({text:'**Жирный текст**\\n\\n<img src=x onerror="window.injected=1">'}), turn);
    }""")
    playwright.expect(page.locator("#chat .stream strong")).to_have_text("Жирный текст")
    assert page.evaluate("window.injected") is None
    assert page.locator("#chat img[onerror]").count() == 0


def test_chat_does_not_jump_when_reading_earlier_messages(page):
    page.evaluate("""() => {
      for (let i=0; i<80; i++) userRow('Сообщение ' + i);
      chat.scrollTop = 0;
      chat.dispatchEvent(new Event('scroll'));
      toolRow('start', {tool:'read', call:1, args:{path:'a.py'}});
      toolRow('end', {tool:'read', call:1, output:'ok'});
    }""")
    assert page.locator("#chat").evaluate("el => el.scrollTop") == 0


def test_history_keeps_the_full_tool_output(page):
    page.evaluate("historyToolRow('[tool result] tool=read error=False\\n' + 'x'.repeat(5000))")
    page.locator('#chat .activity-summary').click()
    page.locator("#chat .tool summary").click()
    assert len(page.locator("#chat .tool pre").inner_text()) == 5000


@pytest.mark.parametrize("skin,theme", [("brutal", "paper"), ("brutal", "zinc"),
                                         ("paper", "paper"), ("paper", "zinc")])
def test_tool_layout_in_each_theme_at_minimum_window_width(page, skin, theme):
    page.set_viewport_size({"width": 760, "height": 600})
    page.evaluate("([skin, theme]) => { applySkin(skin); applyTheme(theme); }", [skin, theme])
    page.wait_for_function("skin => document.querySelector('#skinlink').sheet?.href.endsWith('/'+skin+'.css')", arg=skin)
    page.evaluate("""() => {
      const box = assistantBox();
      box.stream.replaceWith(renderAssistant('```python\\nprint(42)\\n```'));
      toolRow('start', {tool:'bash',call:42,args:{command:'long-command-'.repeat(80)}});
      toolRow('end', {tool:'bash',call:42,error:true,output:'Ошибка'});
    }""")
    card = page.locator("#chat .tool")
    frame = page.locator("#chat .code-block")
    playwright.expect(frame.locator(":scope > .codehead")).to_have_count(1)
    playwright.expect(frame.locator(":scope > pre")).to_have_count(1)
    assert frame.evaluate("el => getComputedStyle(el).overflow") == "hidden"
    assert frame.locator("pre").evaluate("el => getComputedStyle(el).borderTopLeftRadius") == "0px"
    playwright.expect(card.locator(".tool-status")).to_have_text("Ошибка")
    assert page.locator("#chat").evaluate("el => el.scrollWidth <= el.clientWidth")


def test_subagent_expansion_shows_read_only_activity(page):
    page.evaluate("""() => {
      const old = window.fetch;
      window.fetch = async (url, opts) => String(url).startsWith('/api/subagent?')
        ? new Response(JSON.stringify({id:'123456abcdef', task:'Inspect src', status:'running',
          events:[{type:'tool_start',tool:'read',args:{path:'src/app.js'}},
                  {type:'stream_delta',text:'Checking the file'}]}), {status:200})
        : old(url, opts);
      toolRow('start', {tool:'subagent', call:99, args:{task:'Inspect src'}});
      toolRow('end', {tool:'subagent', call:99, output:JSON.stringify({id:'123456abcdef',status:'running'})});
    }""")
    page.locator('#chat .activity-summary').click()
    page.locator('.tool summary').last.click()
    playwright.expect(page.locator('.subagent-chat')).to_contain_text('Inspect src')
    playwright.expect(page.locator('.subagent-chat')).to_contain_text('Checking the file')
    playwright.expect(page.locator('.subagent-chat')).to_contain_text('только просмотр')
    assert page.locator('.subagent-chat textarea, .subagent-chat input, .subagent-chat button').count() == 0


def test_context_bar_uses_model_window_and_discloses_input_cap(page):
    page.evaluate("paintBar(2, 20000, 1000000, 28672, true)")
    playwright.expect(page.locator('#ctxtext')).to_have_text('Контекст: ≈20k / ~1M · 2% · вход до 29k')
    assert 'история может быть сокращена' in page.locator('#ctxtext').get_attribute('title')


def test_application_menu_follows_theme_and_dispatches_actions(page):
    page.evaluate("""() => {
      window.menuActions = [];
      window.beeDesk = {menuAction: action => window.menuActions.push(action)};
    }""")
    page.evaluate("applyTheme('zinc')")
    dark = page.locator('#app-menu').evaluate('el => getComputedStyle(el).backgroundColor')
    page.locator('.app-menu-group summary').nth(1).click()
    page.locator('[data-menu-action="copy"]').click()
    assert page.evaluate('window.menuActions') == ['copy']
    page.locator('.app-menu-group summary').nth(3).click()
    page.locator('[data-menu-action="instructions"]').click()
    playwright.expect(page.locator('#settings-back')).not_to_have_class('hidden')
    playwright.expect(page.locator('[data-tab="instructions"]')).to_have_class('on')
    page.evaluate("applyTheme('paper')")
    light = page.locator('#app-menu').evaluate('el => getComputedStyle(el).backgroundColor')
    assert dark != light


def test_menu_sidebar_search_and_keyboard_shortcuts(page):
    page.evaluate("window.menuActions = []; window.beeDesk = {menuAction: action => window.menuActions.push(action)}")
    page.keyboard.press('Control+Shift+S')
    playwright.expect(page.locator('#sidebar')).not_to_be_visible()
    page.keyboard.press('Control+f')
    playwright.expect(page.locator('#sidebar')).to_be_visible()
    playwright.expect(page.locator('#search')).to_be_focused()
    page.keyboard.press('Control+0')
    assert page.evaluate('window.menuActions') == ['zoomReset']
    page.keyboard.press('Control+,')
    playwright.expect(page.locator('#settings-back')).not_to_have_class('hidden')


def test_navigation_buttons_follow_chat_history_and_sidebar(page):
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = async (url, opts) => {
        const path = String(url);
        if (path.startsWith('/api/session?id=')) {
          const id = decodeURIComponent(path.split('=')[1]);
          return new Response(JSON.stringify({id, messages:[{role:'user',content:'Chat ' + id}]}), {status:200});
        }
        if (path.startsWith('/api/usage?')) return new Response('{}', {status:200});
        return original(url, opts);
      };
    }""")
    assert page.locator('#nav-back').is_disabled()
    page.evaluate("switchSession('first')")
    page.evaluate("switchSession('second')")
    playwright.expect(page.locator('#nav-back')).to_be_enabled()
    page.locator('#nav-back').click()
    playwright.expect(page.locator('#chat')).to_contain_text('Chat first')
    playwright.expect(page.locator('#nav-forward')).to_be_enabled()
    page.locator('#nav-forward').click()
    playwright.expect(page.locator('#chat')).to_contain_text('Chat second')
    page.locator('#nav-sidebar').click()
    playwright.expect(page.locator('#sidebar')).not_to_be_visible()
    playwright.expect(page.locator('#nav-sidebar')).to_have_attribute('aria-pressed', 'false')
    page.locator('#nav-sidebar').click()
    playwright.expect(page.locator('#sidebar')).to_be_visible()
    page.evaluate('setBusy(true)')
    assert page.locator('#nav-back').is_disabled()


def test_sidebar_animates_and_respects_reduced_motion(page):
    page.emulate_media(reduced_motion='no-preference')
    page.evaluate("applySkin('paper')")
    page.wait_for_timeout(500)
    initial = page.locator('#sidebar-wrap').bounding_box()['width']
    assert initial == 248
    assert page.locator('#sidebar-wrap').evaluate("el => getComputedStyle(el).transitionDuration") == '0.28s'
    page.locator('#nav-sidebar').click()
    page.wait_for_timeout(80)
    middle = page.locator('#sidebar-wrap').bounding_box()['width']
    assert 0 < middle < initial
    transform = page.locator('#sidebar').evaluate("el => getComputedStyle(el).transform")
    assert transform != 'none' and float(transform.split(',')[4].strip()) < 0
    page.wait_for_timeout(400)
    assert page.locator('#sidebar-wrap').bounding_box()['width'] == 0
    assert page.locator('#sidebar').get_attribute('aria-hidden') == 'true'
    page.locator('#nav-sidebar').click()
    page.wait_for_timeout(400)
    assert page.locator('#sidebar-wrap').bounding_box()['width'] == initial
    page.emulate_media(reduced_motion='reduce')
    assert page.locator('#sidebar-wrap').evaluate("el => getComputedStyle(el).transitionDuration") == '0s'


def test_window_titlebar_follows_theme_and_skin(page):
    page.evaluate("""() => {
      window.titlebarColors = [];
      window.beeDesk = {setTitlebarTheme: (theme, skin) => window.titlebarColors.push([theme, skin])};
      applySkin('paper'); applyTheme('zinc');
    }""")
    assert page.evaluate('window.titlebarColors.at(-1)') == ['zinc', 'paper']
    assert page.locator('#app-menu').evaluate("el => getComputedStyle(el).webkitAppRegion") == 'drag'
    assert page.locator('.app-menu-group').first.evaluate("el => getComputedStyle(el).webkitAppRegion") == 'no-drag'
    page.locator('.app-menu-group summary').first.click()
    playwright.expect(page.locator('[data-menu-action="new-chat"]')).to_be_visible()


def test_title_event_updates_sidebar_and_compact_rows(page):
    page.evaluate("""() => {
      sessionId = '20261111_123456_abcdef';
      sessionCache = [{id:sessionId, messages:2}];
      drawSessions(sessionCache);
      onFrame('event: title\\ndata: ' + JSON.stringify({title:'Приветствие'}), {});
      applyTheme('zinc'); applySkin('brutal');
    }""")
    playwright.expect(page.locator('#sessions .session .t')).to_have_text('Приветствие')
    assert page.locator('#app-menu').evaluate('el => getComputedStyle(el).backgroundColor') == 'rgb(27, 27, 27)'
    assert page.locator('#sessions .session').evaluate('el => getComputedStyle(el).paddingTop') == '4px'


def test_agent_accent_changes_paper_controls_and_survives_model_change(page):
    page.evaluate("""() => {
      applySkin('paper'); applyTheme('zinc');
      applyUiPrefs({version:11, updated_at:9999999999, skin:'paper', theme:'zinc', accent:'#cc2233'});
    }""")
    button = page.locator('#new-chat')
    playwright.expect(button).to_have_css('background-color', 'rgb(204, 34, 51)')
    playwright.expect(button).to_have_css('color', 'rgb(255, 255, 255)')
    page.evaluate("setActiveModel('gpt-4'); applyTheme('paper')")
    playwright.expect(button).to_have_css('background-color', 'rgb(204, 34, 51)')
    page.evaluate("applyUiPrefs({version:12, updated_at:9999999999, accent:'#ffff00'})")
    playwright.expect(button).to_have_css('color', 'rgb(20, 20, 20)')
    page.evaluate("applyUiPrefs({version:13, updated_at:9999999999, accent:null})")
    assert button.evaluate('el => getComputedStyle(el).backgroundColor') != 'rgb(255, 255, 0)'


def test_tool_expands_and_collapses_with_animation(page):
    page.emulate_media(reduced_motion='no-preference')
    page.evaluate("toolRow('end', {tool:'read', call:900, output:'file contents'})")
    page.locator('#chat .activity-summary').click()
    block = page.locator('#chat details.tool').last
    block.locator('summary').click()
    assert block.evaluate("el => el.getAnimations().some(a => a.effect.getTiming().duration === 240)")
    page.wait_for_timeout(300)
    assert block.evaluate('el => el.open')
    playwright.expect(block.locator('pre')).to_have_text('file contents')
    block.locator('summary').click()
    assert block.evaluate("el => el.getAnimations().some(a => a.effect.getTiming().duration === 240)")
    page.wait_for_timeout(300)
    assert not block.evaluate('el => el.open')
    page.emulate_media(reduced_motion='reduce')
    block.locator('summary').click()
    assert block.evaluate('el => el.open')
    assert not block.evaluate("el => el.getAnimations().some(a => a.effect.getTiming().duration === 240)")


def test_model_color_marker_without_spaces_is_rendered(page):
    page.evaluate("""() => { const node=renderMarkdown('/с.redЧестно про косяк:/с.'); document.getElementById('chat').append(node); }""")
    mark = page.locator('#chat mark.hl-red')
    playwright.expect(mark).to_have_text('Честно про косяк:')
    assert '/с.' not in page.locator('#chat').inner_text()


def test_explicit_format_markers_without_spaces(page):
    page.evaluate("""() => { const node=renderMarkdown('/bПривет/b /iтекст/i /uважно/u'); document.getElementById('chat').append(node); }""")
    playwright.expect(page.locator('#chat strong')).to_have_text('Привет')
    playwright.expect(page.locator('#chat em')).to_have_text('текст')
    playwright.expect(page.locator('#chat u')).to_have_text('важно')


def test_chat_map_previews_and_navigates_long_conversation(page):
    page.evaluate("""() => {
      applyTheme('zinc');
      for (let i=0; i<18; i++) {
        userRow('Вопрос номер ' + i);
        const answer=assistantBox();
        answer.stream.textContent='Ответ на вопрос ' + i + ': ' + 'подробности '.repeat(35);
      }
    }""")
    ticks = page.locator('#chat-map .chat-map-tick')
    playwright.expect(ticks).to_have_count(18)
    ticks.nth(2).hover()
    preview = page.locator('#chat-map-preview')
    playwright.expect(preview).to_be_visible()
    playwright.expect(preview.locator('.chat-map-title')).to_have_text('Вопрос номер 2')
    playwright.expect(preview.locator('.chat-map-answer')).to_contain_text('Ответ на вопрос 2')
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-chat-map.png'), animations='disabled')
    ticks.nth(2).click()
    playwright.expect(ticks.nth(2)).to_have_attribute('aria-current', 'location')
    page.evaluate("document.getElementById('chat').replaceChildren()")
    playwright.expect(page.locator('#chat-map')).to_be_hidden()


def test_reopened_history_groups_tools_without_empty_assistant_blocks(page):
    page.evaluate("""() => {
      const old = window.fetch;
      window.fetch = async (url, opts) => String(url).startsWith('/api/session?id=')
        ? new Response(JSON.stringify({id:'history', messages:[
          {role:'user', content:'Проверь файлы'},
          {role:'assistant', content:''},
          {role:'tool', content:'[tool result] tool=bash error=False\\nfirst'},
          {role:'assistant', content:'   '},
          {role:'tool', content:'[tool result] tool=bash error=False\\nsecond'},
          {role:'assistant', content:'```json\\n{\\"tool\\":\\"think\\",\\"args\\":{\\"plan\\":\\"inspect\\"}}\\n```'},
          {role:'tool', content:'[tool result] tool=think error=False\\nplan'},
          {role:'assistant', content:'Теперь проверю результат.'},
          {role:'assistant', content:''},
          {role:'tool', content:'[tool result] tool=read error=False\\nfile'}
        ]}), {status:200}) : old(url, opts);
      return switchSession('history');
    }""")
    groups = page.locator('#chat > .tool-group')
    playwright.expect(groups).to_have_count(1)
    playwright.expect(groups.nth(0).locator('details.tool')).to_have_count(4)
    playwright.expect(page.locator('#chat > .msg.assistant')).to_have_count(1)
    playwright.expect(page.locator('#chat > .msg.assistant')).to_contain_text('Теперь проверю результат.')
    assert page.locator('#chat .toolcall').count() == 0


def test_reopened_history_preserves_text_and_tool_examples(page):
    page.evaluate("""() => {
      const call='```json\\n{\\"tool\\":\\"read\\",\\"args\\":{}}\\n```';
      window.exampleOnly = historyAssistantText(call, false);
      window.afterCall = historyAssistantText(call + '\\nПроверяю файл.', true);
      window.prose = historyAssistantText('Обычный ответ', true);
    }""")
    assert '```json' in page.evaluate('window.exampleOnly')
    assert page.evaluate('window.afterCall') == 'Проверяю файл.'
    assert page.evaluate('window.prose') == 'Обычный ответ'


def test_project_tab_creates_project_folder_and_chat_and_opens_readonly_agent(page):
    page.evaluate("""() => {
      const fallback = window.fetch;
      window.projectRows = [];
      window.fetch = async (url, opts) => {
        const p = String(url), body = opts && opts.body ? JSON.parse(opts.body) : {};
        let result;
        if (p === '/api/projects' && opts?.method === 'POST') {
          if (body.action === 'create') {
            const project={id:'project1',name:body.name,folders:[],chats:[],agents:[]};
            window.projectRows.push(project); result={item:project};
          } else {
            const folder={id:'folder1',name:body.name,parent:body.parent||''};
            window.projectRows[0].folders.push(folder); result={item:folder};
          }
        } else if (p === '/api/projects') result={projects:window.projectRows};
        else if (p === '/api/sessions/new') {
          window.createdIn = body;
          window.projectRows[0].chats.push({id:'test-chat',folder:body.folder||''});
          result={id:'test-chat'};
        } else if (p.startsWith('/api/subagent?id=')) result={id:'abcdef123456', task:'Исследуй код', status:'done', result:'Готово', events:[]};
        else return fallback(url, opts);
        return new Response(JSON.stringify(result), {status:200,headers:{'Content-Type':'application/json'}});
      };
    }""")
    page.evaluate('refreshProjects(true)')
    playwright.expect(page.locator('#projects-panel')).to_be_visible()
    playwright.expect(page.locator('.recent-heading')).to_be_visible()
    page.locator('#project-create').click()
    page.locator('#project-name').fill('Мой проект')
    page.locator('#project-form button[type=submit]').click()
    playwright.expect(page.locator('#projects-list')).to_contain_text('Мой проект')
    page.locator('.project-line').first.get_by_role('button', name='Создать папку').click()
    page.locator('#project-name').fill('Дизайн')
    page.locator('#project-form button[type=submit]').click()
    playwright.expect(page.locator('#projects-list')).to_contain_text('Дизайн')
    page.locator('.project-line').filter(has_text='Дизайн').get_by_role('button', name='Создать чат').click()
    playwright.expect(page.locator('#projects-list')).to_contain_text('Чат test-chat')
    assert page.evaluate('window.createdIn') == {'project':'project1', 'folder':'folder1'}
    page.evaluate("""() => {
      window.projectRows[0].agents.push({id:'abcdef123456',task:'Исследуй код',folder:'folder1',status:'done'});
      setBusy(true); return refreshProjects(true);
    }""")
    page.evaluate("applyTheme('zinc')")
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-projects.png'), animations='disabled')
    page.locator('.project-agent').click()
    playwright.expect(page.locator('#agent-view')).to_be_visible()
    playwright.expect(page.locator('#agent-view-feed')).to_contain_text('Исследуй код')
    playwright.expect(page.locator('#agent-view-feed')).to_contain_text('Готово')
    playwright.expect(page.locator('#composer-wrap')).to_be_hidden()
    page.locator('#agent-view-close').click()
    playwright.expect(page.locator('#composer-wrap')).to_be_visible()


@pytest.mark.parametrize('skin,theme', [('brutal', 'zinc'), ('paper', 'zinc'), ('brutal', 'paper'), ('paper', 'paper')])
def test_project_form_matches_appearance(page, skin, theme):
    page.evaluate("([skin, theme]) => { applySkin(skin); applyTheme(theme); }", [skin, theme])
    page.locator('#project-create').click()
    playwright.expect(page.locator('#project-name')).to_be_focused()
    playwright.expect(page.locator('#project-form')).to_be_visible()
    page.screenshot(path=str(Path(tempfile.gettempdir()) / f'preview-project-form-{skin}-{theme}.png'), animations='disabled')
    page.locator('#project-name').press('Escape')
    playwright.expect(page.locator('#project-form')).to_be_hidden()
    playwright.expect(page.locator('#project-create')).to_be_focused()


def test_stray_title_markers_are_rendered_as_headings_without_touching_code(page):
    page.evaluate("""() => {
      const target=document.createElement('div'); target.id='marker-regression';
      target.append(renderAssistant('Проверено.\\n\\n█Что умеет█\\n\\n```text\\n█пример█\\n```'));
      document.body.append(target);
    }""")
    root = page.locator('#marker-regression')
    playwright.expect(root.locator('strong')).to_have_text('Что умеет')
    assert '█Что умеет█' not in root.inner_text()
    assert '█пример█' in root.locator('pre').inner_text()


def test_response_formatting_supports_markdown_and_existing_slash_tags(page):
    page.evaluate("""() => {
      const target=document.createElement('div'); target.id='format-regression';
      target.append(renderAssistant('**Жирный** и /с.redВыделено/с.')); document.body.append(target);
    }""")
    playwright.expect(page.locator('#format-regression strong')).to_have_text('Жирный')
    playwright.expect(page.locator('#format-regression mark')).to_have_text('Выделено')


def test_malformed_model_heading_is_repaired_in_prose_only(page):
    page.evaluate("""() => {
      const target=document.createElement('div'); target.id='mode-regression';
      target.append(renderAssistant('.modeТекущее состояние/:\\n\\n```text\\n.modeТекущее состояние/:\\n```'));
      document.body.append(target);
    }""")
    playwright.expect(page.locator('#mode-regression strong')).to_have_text('Текущее состояние:')
    assert '.modeТекущее состояние/:' in page.locator('#mode-regression pre').inner_text()


def test_activity_summary_and_model_caption_streaming(page):
    page.evaluate("""() => {
      userRow('Создай окно');
      window.workTurn={box:assistantBox(),hadText:false};
      const e=(kind,data)=>onFrame('event: '+kind+'\\ndata: '+JSON.stringify(data),window.workTurn);
      e('delta',{text:'⟦дей'}); e('delta',{text:'ствие: Делаю окно больше⟧\\n'});
      e('tool',{phase:'start',tool:'edit',call:101,args:{path:'app.py'}});
      e('tool',{phase:'end',tool:'edit',call:101,output:'ok'});
      window.firstCaption=document.querySelector('.activity-label').textContent;
      e('tool',{phase:'start',tool:'bash',call:102,args:{command:'pytest'}});
    }""")
    group=page.locator('#chat .tool-group')
    assert page.evaluate('window.firstCaption') == 'Делаю окно больше'
    playwright.expect(group).to_have_count(1)
    playwright.expect(group.locator('.activity-label')).to_have_text('Выполняет команду')
    playwright.expect(group.locator('.activity-count')).to_have_text('2')
    playwright.expect(group.locator('.tool')).to_have_count(2)
    playwright.expect(group.locator('.tool').first).not_to_be_visible()
    playwright.expect(page.locator('#chat')).not_to_contain_text('⟦действие:')
    group.locator('.activity-summary').click()
    playwright.expect(group.locator('.tool').first).to_be_visible()
    page.evaluate("""() => onFrame('event: tool\\ndata: '+JSON.stringify({phase:'end',tool:'bash',call:102,error:true,output:'bad'}),window.workTurn)""")
    playwright.expect(group.locator('.activity-label')).to_contain_text('Не удалось')
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-activity.png'), animations='disabled')


def test_activity_marker_does_not_consume_code(page):
    result = page.evaluate("""() => extractActivity('```text\\n⟦действие: пример⟧\\n```', true)""")
    assert '⟦действие: пример⟧' in result['text'] and not result['label']


def test_activity_group_animates_open_and_close_and_respects_reduced_motion(page):
    page.emulate_media(reduced_motion='no-preference')
    page.evaluate("toolRow('end', {tool:'read',call:801,output:'data'})")
    group = page.locator('#chat .tool-group')
    summary = group.locator('.activity-summary')
    summary.click()
    assert group.locator('.tool-group-items').evaluate("el => el.getAnimations().some(a => a.effect.getTiming().duration === 300)")
    page.wait_for_timeout(350)
    assert group.evaluate('el => el.open')
    summary.click()
    assert group.locator('.tool-group-items').evaluate("el => el.getAnimations().some(a => a.effect.getTiming().duration === 300)")
    page.wait_for_timeout(350)
    assert not group.evaluate('el => el.open')
    page.emulate_media(reduced_motion='reduce')
    summary.click()
    assert group.evaluate('el => el.open')
    assert not group.locator('.tool-group-items').evaluate("el => el.getAnimations().some(a => a.effect.getTiming().duration === 300)")


def test_activity_opening_really_changes_height_over_time(page):
    page.emulate_media(reduced_motion='no-preference')
    page.evaluate("""() => {
      userRow('test');
      for (let i=0;i<6;i++) toolRow('end',{tool:'read',call:900+i,output:'ok'});
    }""")
    group=page.locator('#chat .tool-group')
    start=group.bounding_box()['height']
    group.locator('.activity-summary').click()
    page.locator('.tool-group-items').evaluate("el => { const a=el.getAnimations()[0]; a.pause(); a.currentTime=100; }")
    middle=group.bounding_box()['height']
    page.locator('.tool-group-items').evaluate("el => { const a=el.getAnimations()[0]; a.currentTime=299; }")
    end=group.bounding_box()['height']
    assert start + 5 < middle < end - 5, (start,middle,end)
    page.locator('.tool-group-items').evaluate("el => el.getAnimations()[0].finish()")
    group.locator('.activity-summary').click()
    page.locator('.tool-group-items').evaluate("el => { const a=el.getAnimations()[0]; a.pause(); a.currentTime=100; }")
    middle_close=group.bounding_box()['height']
    page.locator('.tool-group-items').evaluate("el => { const a=el.getAnimations()[0]; a.currentTime=299; }")
    closed=group.bounding_box()['height']
    assert closed + 5 < middle_close < end - 5, (closed,middle_close,end)
    page.locator('.tool-group-items').evaluate("el => el.getAnimations()[0].finish()")
    page.wait_for_timeout(50)
    group.locator('.activity-summary').click()
    page.wait_for_timeout(45)
    group.locator('.activity-summary').click()
    page.wait_for_timeout(350)
    assert not group.evaluate('el => el.open')


def test_reasoning_picker_uses_model_levels_and_sends_choice(page):
    page.evaluate("""() => {
      modelsProvider='demo'; model='alpha';
      models=[{id:'alpha',reasoning:true,effortSupported:true,efforts:['low','high']},
              {id:'beta',reasoning:false,effortSupported:true,efforts:[]}];
      setActiveModel('alpha');
      const old=window.fetch;
      window.fetch=async (url, opts) => {
        if (String(url)==='/api/chat') {
          window.chatPayload=JSON.parse(opts.body);
          return new Response('event: done\\ndata: {"text":"Готово"}\\n\\n', {status:200});
        }
        return old(url, opts);
      };
    }""")
    select=page.locator('#effort-picker')
    playwright.expect(select).to_be_visible()
    page.locator('#effort-trigger').click()
    page.locator('#effort-menu button[data-value="high"]').click()
    page.locator('#input').fill('Проверь ответ')
    page.locator('#send').click()
    page.wait_for_function('window.chatPayload !== undefined')
    assert page.evaluate('window.chatPayload.reasoning_effort') == 'high'
    page.evaluate("model='beta'; setActiveModel('beta')")
    playwright.expect(select).to_be_hidden()
    page.evaluate("model='alpha'; setActiveModel('alpha')")
    playwright.expect(page.locator('#effort-label')).to_have_text('high')
    page.evaluate("applySkin('paper'); applyTheme('zinc')")
    page.locator('#effort-trigger').click()
    playwright.expect(page.locator('#effort-menu button[data-value="high"]')).to_have_attribute('aria-selected', 'true')
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-reasoning-menu.png'), animations='disabled')
    page.locator('#effort-trigger').focus()
    page.keyboard.press('ArrowDown')
    playwright.expect(page.locator('#effort-menu button').first).to_be_focused()
    page.keyboard.press('Escape')
    playwright.expect(select).not_to_have_attribute('open', '')


@pytest.mark.parametrize('skin,theme', [('brutal','paper'),('brutal','zinc'),('paper','paper'),('paper','zinc')])
def test_reasoning_menu_uses_theme_and_stays_visible(page, skin, theme):
    page.evaluate("""([skin,theme]) => {
      applySkin(skin); applyTheme(theme); modelsProvider='visual'; model='m';
      models=[{id:'m',reasoning:true,effortSupported:true,efforts:['minimal','low','medium','high','xhigh','max','ultra']}];
      setActiveModel('m');
    }""", [skin, theme])
    page.locator('#effort-trigger').click()
    menu=page.locator('#effort-menu')
    playwright.expect(menu).to_be_visible()
    rect=menu.bounding_box(); assert rect['x'] >= 0 and rect['y'] >= 0
    assert rect['x']+rect['width'] <= 1280 and rect['y']+rect['height'] <= 900
    assert menu.evaluate('el => getComputedStyle(el).backgroundColor') != 'rgba(0, 0, 0, 0)'
    page.locator('#input').click()
    playwright.expect(page.locator('#effort-picker')).not_to_have_attribute('open','')


def test_builtin_skill_is_marked_in_extensions(page):
    page.evaluate("""() => {
      const old=window.fetch;
      window.fetch=async (url,opts) => String(url)==='/api/extensions'
        ? new Response(JSON.stringify({skills:[{name:'skill-finder',description:'Поиск и установка навыков',builtin:true}],servers:[],errors:[]}), {status:200,headers:{'Content-Type':'application/json'}})
        : old(url,opts);
      applySkin('paper'); applyTheme('zinc');
    }""")
    page.locator('#gear').click()
    page.locator('[data-tab="extensions"]').click()
    playwright.expect(page.locator('#extension-skills')).to_contain_text('skill-finder')
    playwright.expect(page.locator('#extension-skills .builtin-skill-badge')).to_have_text('Встроен')
    page.screenshot(path=str(Path(tempfile.gettempdir()) / 'preview-skill-finder.png'), animations='disabled')
