(() => {
  const waiting = new Map(); let sequence = 0;
  const invoke = (action, data = {}) => new Promise((resolve) => {
    const id = ++sequence; waiting.set(id, resolve);
    window.ipc.postMessage(JSON.stringify({id, action, ...data}));
    setTimeout(() => { if (waiting.delete(id)) resolve(null); }, 120000);
  });
  window.__beeResolve = (id, result) => { const done = waiting.get(id); if (done) { waiting.delete(id); done(result); } };
  window.beeDesk = {
    attach: () => invoke('attach'),
    openExternal: url => invoke('openExternal', {url}),
    setTitlebarTheme: () => Promise.resolve(true),
    menuAction: action => {
      if (action === 'paste') return invoke('clipboardPaste').then(text => typeof text === 'string' && document.execCommand('insertText', false, text));
      if (['undo','redo','cut','copy','selectAll'].includes(action)) {
        try { return Promise.resolve(document.execCommand(action)); } catch { return Promise.resolve(false); }
      }
      return invoke('menuAction', {value: action});
    }
  };
  document.addEventListener('DOMContentLoaded', () => {
    const bar = document.getElementById('app-menu'); if (!bar) return;
    const controls = document.createElement('div'); controls.className = 'rust-window-controls';
    for (const [action, label, symbol] of [['minimize','Свернуть','—'], ['maximize','Развернуть','□'], ['close','Закрыть','×']]) {
      const button = document.createElement('button'); button.type = 'button'; button.title = label;
      button.setAttribute('aria-label', label); button.textContent = symbol;
      button.onclick = () => invoke('window', {value: action}); controls.append(button);
    }
    bar.append(controls);
    bar.addEventListener('mousedown', event => {
      if (event.button === 0 && !event.target.closest('button, summary, .app-menu-popup')) {
        if (event.detail === 2) invoke('window', {value:'maximize'});
        else invoke('window', {value:'drag'});
      }
    });
    const style = document.createElement('style'); style.textContent = `
      #app-menu { padding-right: 150px; }
      .rust-window-controls { position:absolute; right:0; top:0; height:35px; display:flex; }
      .rust-window-controls button { width:46px; height:35px; border:0; background:transparent; color:var(--ink); cursor:pointer; font:16px system-ui; }
      .rust-window-controls button:hover { background:var(--sand); }
      .rust-window-controls button:last-child:hover { background:#c42b1c; color:white; }
      .rust-resize { position:fixed; z-index:9999; }
      .rust-resize-north, .rust-resize-south { left:6px; right:6px; height:5px; cursor:ns-resize; }
      .rust-resize-north { top:0; } .rust-resize-south { bottom:0; }
      .rust-resize-east, .rust-resize-west { top:6px; bottom:6px; width:5px; cursor:ew-resize; }
      .rust-resize-east { right:0; } .rust-resize-west { left:0; }
      .rust-resize-northwest, .rust-resize-northeast, .rust-resize-southwest, .rust-resize-southeast { width:8px; height:8px; }
      .rust-resize-northwest { left:0; top:0; cursor:nwse-resize; } .rust-resize-northeast { right:0; top:0; cursor:nesw-resize; }
      .rust-resize-southwest { left:0; bottom:0; cursor:nesw-resize; } .rust-resize-southeast { right:0; bottom:0; cursor:nwse-resize; }
    `; document.head.append(style);
    for (const edge of ['north','south','east','west','northwest','northeast','southwest','southeast']) {
      const handle = document.createElement('div'); handle.className = 'rust-resize rust-resize-' + edge;
      handle.addEventListener('mousedown', event => { if (event.button === 0) invoke('resize', {value: edge}); });
      document.body.append(handle);
    }
  });
})();
