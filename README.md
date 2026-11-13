# BeeCode

BeeCode is an AI coding agent for the terminal and Windows desktop. Describe a task in plain language and let the agent inspect files, make changes, run commands and report what it did.

![BeeCode desktop: project sidebar and chat](docs/screenshots/desktop-chat.png)

*Illustrative session with demonstration data and translated interface labels.*

## What you can do

- **Work with your code:** read and edit files, search a project and run commands, with configurable tool permissions.
- **Choose your models:** connect an OpenAI-compatible provider or use the available built-in providers. Supported models and costs depend on the provider.
- **Extend the agent:** use skills, plugins and MCP servers. The desktop includes `skill-finder` to search for and install skills and `customuze beecode` to adjust the interface and additional instructions.
- **Organize work:** the desktop groups chats and subagents into projects. Project folders are chat groups; they do not create folders on disk.
- **Follow along:** expand the activity summary to inspect tool calls and their results. Desktop subagent chats are available in read-only view.

![BeeCode desktop: expanded tool activity](docs/screenshots/desktop-tools.png)

## Install

### Windows desktop

1. Download [BeeCode.Rust.Setup.5.0.2.exe](https://github.com/egorVasile/beecode/releases/latest) from Releases and run it.
2. Open BeeCode, then choose a provider and model in **Settings → Providers**.
3. Start a chat. The default working folder is `Documents/BeeCode Clean`. For supported OpenAI-compatible models, configure the available reasoning levels in the provider settings to select one beside the model.

The desktop uses a Rust shell and WebView2. It needs Python 3.10 or newer; on first launch, BeeCode creates a Python environment and may download dependencies. Desktop and CLI versions are numbered separately.

### Terminal

Requires Python 3.10 or newer:

```bash
pip install git+https://github.com/egorVasile/beecode.git
cd path/to/your/project
beecode
```

Use `/help` for commands, `/providers` and `/models` to inspect available options, `/permissions` to manage tool access, and `/mcp` or `/plugins` to manage extensions. Run `beecode --continue` to resume the last session.

## Local data and source

BeeCode keeps configuration and conversation history locally. Add `beeagent.json` and `.beeagent/` to your project's `.gitignore`: they can contain provider keys, chats and tool output.

The CLI source is in [`beeagent/`](beeagent/). The Windows desktop, its bundled agent and build files are in [`desktop/`](desktop/).

## License

[GPL-3.0-or-later](LICENSE).
