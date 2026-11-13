#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{env, fs, io::{BufRead, BufReader, Read}, path::{Path, PathBuf}, process::{Child, Command, Stdio}, sync::mpsc, thread, time::Duration};
use serde::Deserialize;
use serde_json::{json, Value};
use tao::{event::{Event, WindowEvent}, event_loop::{ControlFlow, EventLoopBuilder}, window::WindowBuilder, dpi::LogicalSize};
use url::Url;
use wry::{WebContext, WebViewBuilder, NewWindowResponse};
#[cfg(feature = "test-automation")]
use wry::WebViewBuilderExtWindows;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

struct AgentProcess(Child);
impl Drop for AgentProcess {
    fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); }
}

#[derive(Debug, Deserialize)]
struct Request { id: u64, action: String, value: Option<String>, url: Option<String> }
#[derive(Debug)]
enum Message { Request(Request) }

fn hidden(command: &mut Command) -> &mut Command {
    #[cfg(windows)] command.creation_flags(0x08000000);
    command
}
fn resource_dir() -> Result<PathBuf, String> {
    let exe = env::current_exe().map_err(|e| e.to_string())?;
    let base = exe.parent().ok_or("No executable directory")?;
    let candidates = [base.join("resources"), base.to_path_buf(), PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf()];
    candidates.into_iter().find(|p| p.join("ui/index.html").is_file() && p.join("agent-src/beeagent/serve.py").is_file())
        .ok_or_else(|| "BeeCode UI or agent resources are missing".into())
}
fn success(command: &mut Command) -> bool { hidden(command).stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false) }
fn start_server(resources: &Path) -> Result<(Child, u16), String> {
    let home = PathBuf::from(env::var_os("USERPROFILE").ok_or("USERPROFILE is missing")?);
    let venv = home.join(".beecode").join("venv");
    let python = venv.join("Scripts/python.exe");
    if !python.is_file() {
        let base = env::var("BEECODE_PYTHON").unwrap_or_else(|_| "python".into());
        if !success(Command::new(base).args(["-m", "venv"]).arg(&venv)) { return Err("Не удалось создать окружение Python. Установите Python 3.10 или новее.".into()); }
    }
    let workdir = env::var_os("BEECODE_WORKDIR").map(PathBuf::from).unwrap_or_else(|| home.join("Documents/BeeCode Clean"));
    fs::create_dir_all(&workdir).map_err(|e| format!("Рабочая папка: {e}"))?;
    let src = resources.join("agent-src");
    let old = env::var_os("PYTHONPATH").unwrap_or_default();
    let mut paths = vec![src.clone()]; paths.extend(env::split_paths(&old));
    let pythonpath = env::join_paths(paths).map_err(|e| e.to_string())?;
    let configure = |command: &mut Command| { command.env("PYTHONPATH", &pythonpath).env("PYTHONUTF8", "1").env("TERM_PROGRAM", "beecode-desktop"); };
    let mut check = Command::new(&python); configure(&mut check); check.args(["-c", "import beeagent.serve"]);
    if !success(&mut check) {
        let mut install = Command::new(&python); configure(&mut install); install.args(["-m", "pip", "install", "--upgrade", "--disable-pip-version-check"]).arg(&src);
        if !success(&mut install) { return Err("Не удалось установить зависимости агента BeeCode.".into()); }
    }
    let mut command = Command::new(&python); configure(&mut command);
    command.args(["-u", "-m", "beeagent.serve", "--port", "0", "--workdir"]).arg(&workdir).arg("--ui").arg(resources.join("ui"))
        .current_dir(&workdir).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
    let mut child = hidden(&mut command).spawn().map_err(|e| format!("Не удалось запустить агент: {e}"))?;
    let stdout = child.stdout.take().ok_or("No agent output")?;
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || { for line in BufReader::new(stdout).lines().map_while(Result::ok) {
        if let Some(rest) = line.strip_prefix("BEECODE_SERVE port=") {
            if let Ok(port) = rest.split_whitespace().next().unwrap_or("").parse::<u16>() { let _ = tx.send(port); }
        }
    }});
    match rx.recv_timeout(Duration::from_secs(60)) { Ok(port) => Ok((child, port)), Err(_) => { let _ = child.kill(); let _ = child.wait(); Err("Агент не запустился за 60 секунд.".into()) } }
}
fn collect_files(paths: Vec<PathBuf>) -> Value {
    const EACH: u64 = 200 * 1024;
    const TOTAL: u64 = 5 * 1024 * 1024;
    let mut files = Vec::new(); let mut total = 0u64;
    for path in paths {
        let Ok(file) = fs::File::open(&path) else { continue };
        if !file.metadata().map(|meta| meta.is_file()).unwrap_or(false) { continue }
        let mut raw = Vec::new();
        if file.take(EACH + 1).read_to_end(&mut raw).is_err() || raw.len() as u64 > EACH || raw.contains(&0) { continue }
        if total + raw.len() as u64 > TOTAL { break }
        let Ok(text) = String::from_utf8(raw) else { continue };
        total += text.len() as u64;
        files.push(json!({"path": path.to_string_lossy(), "content": text}));
    }
    json!(files)
}
fn read_files() -> Value {
    let Some(paths) = rfd::FileDialog::new().set_title("Прикрепить файлы").pick_files() else { return json!([]) };
    collect_files(paths)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn attachments_reject_binary_oversized_and_invalid_utf8() {
        let dir = tempfile::tempdir().unwrap();
        let good = dir.path().join("good.txt");
        let binary = dir.path().join("binary.txt");
        let oversized = dir.path().join("large.txt");
        let invalid = dir.path().join("invalid.txt");
        fs::write(&good, "Привет").unwrap();
        fs::write(&binary, b"a\0b").unwrap();
        fs::write(&oversized, vec![b'x'; 200 * 1024 + 1]).unwrap();
        fs::write(&invalid, [0xff]).unwrap();
        let result = collect_files(vec![good, binary, oversized, invalid]);
        let rows = result.as_array().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["content"], "Привет");
    }
}

fn run() -> Result<(), String> {
    let resources = resource_dir()?;
    let (child, port) = start_server(&resources)?;
    let mut child = AgentProcess(child);
    let event_loop = EventLoopBuilder::<Message>::with_user_event().build();
    let window = WindowBuilder::new().with_title("BeeCode Desktop").with_inner_size(LogicalSize::new(1180.0, 780.0))
        .with_min_inner_size(LogicalSize::new(760.0, 500.0)).with_decorations(false).build(&event_loop).map_err(|e| e.to_string())?;
    let proxy = event_loop.create_proxy();
    let origin = format!("http://127.0.0.1:{port}");
    let safe_origin = origin.clone(); let navigation_origin = origin.clone();
    let profile = env::var_os("LOCALAPPDATA").map(PathBuf::from)
        .ok_or("LOCALAPPDATA is missing")?.join("BeeCode-Rust-Clean").join("WebView2");
    #[cfg(feature = "test-automation")]
    let profile = env::var_os("BEECODE_TEST_WEBVIEW_DIR").map(PathBuf::from).unwrap_or(profile);
    let mut context = WebContext::new(Some(profile));
    let builder = WebViewBuilder::new_with_web_context(&mut context);
    #[cfg(feature = "test-automation")]
    let builder = if let Ok(port) = env::var("BEECODE_TEST_CDP_PORT").unwrap_or_default().parse::<u16>() {
        builder.with_additional_browser_args(format!("--remote-debugging-port={port}"))
    } else { builder };
    let webview = builder.with_url(format!("{origin}/app/"))
        .with_initialization_script(include_str!("bridge.js"))
        .with_navigation_handler(move |url| Url::parse(&url).ok().map(|u| u.origin().ascii_serialization() == navigation_origin).unwrap_or(false))
        .with_new_window_req_handler(|_, _| NewWindowResponse::Deny)
        .with_ipc_handler(move |request| {
            let source = request.uri().to_string();
            if Url::parse(&source).ok().map(|u| u.origin().ascii_serialization() == safe_origin) != Some(true) { return; }
            if request.body().len() > 8192 { return; }
            if let Ok(value) = serde_json::from_str::<Request>(request.body()) { let _ = proxy.send_event(Message::Request(value)); }
        }).build(&window).map_err(|e| format!("WebView2: {e}"))?;
    let mut zoom = 1.0f64;
    event_loop.run(move |event, _, control| {
        *control = ControlFlow::Wait;
        match event {
            Event::WindowEvent {event: WindowEvent::CloseRequested, ..} => { let _ = child.0.kill(); let _ = child.0.wait(); *control = ControlFlow::Exit; },
            Event::UserEvent(Message::Request(req)) => {
                let mut reply = json!(true); let action = req.value.as_deref().unwrap_or("");
                match req.action.as_str() {
                    "window" => match action { "close" => { let _ = child.0.kill(); let _ = child.0.wait(); *control = ControlFlow::Exit; }, "minimize" => window.set_minimized(true), "maximize" => window.set_maximized(!window.is_maximized()), "drag" => { let _ = window.drag_window(); }, _ => reply = json!(false) },
                    "menuAction" => match action {
                        "close" => { let _ = child.0.kill(); let _ = child.0.wait(); *control = ControlFlow::Exit; },
                        "fullscreen" => window.set_fullscreen(if window.fullscreen().is_some() { None } else { Some(tao::window::Fullscreen::Borderless(None)) }),
                        "reload" => { let _ = webview.evaluate_script("location.reload()"); },
                        "zoomIn" | "zoomOut" | "zoomReset" => { zoom = match action {"zoomIn" => (zoom + 0.1).min(2.0), "zoomOut" => (zoom - 0.1).max(0.5), _ => 1.0}; let _ = webview.evaluate_script(&format!("document.body.style.zoom={zoom}")); },
                        _ => reply = json!(false),
                    },
                    "resize" => {
                        let direction = match action {
                            "north" => Some(tao::window::ResizeDirection::North), "south" => Some(tao::window::ResizeDirection::South),
                            "east" => Some(tao::window::ResizeDirection::East), "west" => Some(tao::window::ResizeDirection::West),
                            "northeast" => Some(tao::window::ResizeDirection::NorthEast), "northwest" => Some(tao::window::ResizeDirection::NorthWest),
                            "southeast" => Some(tao::window::ResizeDirection::SouthEast), "southwest" => Some(tao::window::ResizeDirection::SouthWest), _ => None,
                        };
                        reply = json!(direction.map(|d| window.drag_resize_window(d).is_ok()).unwrap_or(false));
                    },
                    "clipboardPaste" => reply = arboard::Clipboard::new().and_then(|mut c| c.get_text()).ok().map(Value::String).unwrap_or(Value::Null),
                    "attach" => reply = read_files(),
                    "openExternal" => { reply = json!(req.url.as_deref().and_then(|x| Url::parse(x).ok()).filter(|u| matches!(u.scheme(), "http" | "https")).map(|u| webbrowser::open(u.as_str()).is_ok()).unwrap_or(false)); },
                    _ => reply = json!(false),
                }
                let script = format!("window.__beeResolve({}, {})", req.id, reply);
                let _ = webview.evaluate_script(&script);
            },
            Event::LoopDestroyed => { let _ = child.0.kill(); let _ = child.0.wait(); },
            _ => (),
        }
    });
}
fn main() { if let Err(error) = run() { rfd::MessageDialog::new().set_title("BeeCode Desktop").set_description(&error).set_level(rfd::MessageLevel::Error).show(); } }
