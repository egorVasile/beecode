"""Borrowing providers from the other agents on the machine.

The scan only reads: nothing is written, moved or sent, and a file that does
not parse is absence rather than an error. Keys travel into the config on an
explicit `/providers import <name>` and reach the screen as tails only — every
test below that touches a key asserts the full value appears nowhere.
"""
import json
import os
from pathlib import Path

from beeagent.config.schema import BeeConfig
from beeagent.core import agent_import as ai


LITERAL_KEY = "sk-test-opencode-aaaabbbbccccdddd"
ENV_KEY = "sk-test-env-1111222233334444"


def _machine(root, monkeypatch):
    """A fake machine under `root`: every agent installed, every file parseable."""
    home = Path(root)
    monkeypatch.setenv("LLM7_API_KEY", ENV_KEY)
    monkeypatch.delenv("MISSING_KEY_HERE", raising=False)
    conf = home / ".config" / "opencode"
    conf.mkdir(parents=True)
    (conf / "opencode.json").write_text(json.dumps({
        "model": "llm7/m",
        "provider": {
            "llm7": {
                "options": {"baseURL": "https://api.llm7.io/v1",
                            "apiKey": "{env:LLM7_API_KEY}"},
                "models": {"m": {}},
            },
            "mine": {
                "options": {"baseURL": "https://mine.test/v1",
                            "apiKey": LITERAL_KEY},
                "models": {"m1": {}, "m2": {}},
            },
            "nokey": {
                "options": {"baseURL": "https://nokey.test/v1",
                            "apiKey": "{env:MISSING_KEY_HERE}"},
                "models": {},
            },
        },
    }), encoding="utf-8")
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text(
        "[model_providers.nova]\n"
        "base_url = \"https://nova.test/v1\"\n"
        "env_key = \"NOVA_API_KEY\"\n", encoding="utf-8")
    (codex / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-test-codex-zzzz"}), encoding="utf-8")
    monkeypatch.setenv("NOVA_API_KEY", "sk-test-nova-qqqq")
    gemini = home / ".gemini"
    gemini.mkdir()
    (gemini / "settings.json").write_text("{}", encoding="utf-8")
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    (home / ".aider.conf.yml").write_text(
        "groq-api-key: sk-test-aider-gggg\n"
        "weird-api-key: sk-test-aider-wwww\n", encoding="utf-8")
    ext = home / ".vscode" / "extensions"
    (ext / "saoudrizwan.cline-nightly-1").mkdir(parents=True)
    (ext / "rooveterinaryinc.roo-cline-2").mkdir(parents=True)
    (home / ".qoder").mkdir()
    (home / ".qoder" / "settings.json").write_text("{}", encoding="utf-8")
    zed = home / ".config" / "zed"
    zed.mkdir(parents=True)
    (zed / "settings.json").write_text("{}", encoding="utf-8")
    return home


def _scan(tmp_path, monkeypatch):
    home = _machine(tmp_path / 'home', monkeypatch)
    return home, {f.id: f for f in ai.scan(home, home)}


def test_opencode_providers_keys_and_env_refs(tmp_path, monkeypatch):
    _machine(tmp_path / 'home', monkeypatch)
    home = tmp_path / "home"
    found = {f.id: f for f in ai.scan(home, home)}["opencode"]
    assert found.found
    by_name = {p.name: p for p in found.providers}
    assert by_name["llm7"].key == ENV_KEY
    assert by_name["llm7"].key_from == "env:LLM7_API_KEY"
    assert by_name["mine"].key == LITERAL_KEY
    assert by_name["mine"].key_from == "file"
    assert by_name["mine"].models == ["m1", "m2"]
    assert by_name["nokey"].key is None
    assert "not set" in by_name["nokey"].note


def test_codex_profiles_and_auth_key(tmp_path, monkeypatch):
    home, findings = _scan(tmp_path, monkeypatch)
    by_name = {p.name: p for p in findings["codex"].providers}
    assert by_name["nova"].key == "sk-test-nova-qqqq"
    assert by_name["nova"].key_from == "env:NOVA_API_KEY"
    assert by_name["nova"].url == "https://nova.test/v1"
    assert by_name["codex-openai"].key == "sk-test-codex-zzzz"


def test_presence_agents_report_without_keys(tmp_path, monkeypatch):
    home, findings = _scan(tmp_path, monkeypatch)
    assert findings["claude"].found and not findings["claude"].providers
    assert "OAuth" in findings["claude"].detail
    assert findings["qoder"].found and findings["zed"].found
    assert findings["gemini"].found
    names = [p.name for p in findings["vscode"].providers]
    assert "cline" in names and "roo" in names


def test_aider_maps_known_keys_and_flags_the_rest(tmp_path, monkeypatch):
    home, findings = _scan(tmp_path, monkeypatch)
    by_name = {p.name: p for p in findings["aider"].providers}
    assert by_name["groq"].key == "sk-test-aider-gggg"
    assert "by hand" in by_name["weird-api-key"].note


def test_env_agent_offers_exported_preset_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-env")
    home = _machine(tmp_path / 'home', monkeypatch)
    found = {f.id: f for f in ai.scan(home, home)}["env"]
    assert found.found
    by_name = {p.name: p for p in found.providers}
    assert by_name["groq"].key == "gsk-test-env"


def test_broken_files_are_absence_not_errors(tmp_path, monkeypatch):
    home = tmp_path / "home"
    conf = home / ".config" / "opencode"
    conf.mkdir(parents=True)
    (conf / "opencode.json").write_text("{oops", encoding="utf-8")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        "[model_providers.\nkey = ", encoding="utf-8")
    findings = {f.id: f for f in ai.scan(home, home)}
    assert findings["opencode"].found is False
    assert findings["codex"].found is True
    assert findings["codex"].providers == []


def test_jsonc_fallback_reads_past_comments(tmp_path, monkeypatch):
    home = tmp_path / "home"
    conf = home / ".config" / "opencode"
    conf.mkdir(parents=True)
    (conf / "opencode.jsonc").write_text(
        "{\n// a comment\n\"provider\": {\n"
        "/* span */\"a\": {\"options\": {\"baseURL\": \"https://a.test/v1\"},"
        " \"models\": {}}}\n}\n",
        encoding="utf-8")
    found = {f.id: f for f in ai.scan(home, home)}["opencode"]
    assert found.found
    assert found.providers[0].url == "https://a.test/v1"


def _ctx(config=None):
    from beeagent.ui.commands import ReplContext

    class Ctx:
        pass

    ctx = Ctx()
    ctx.config = config or BeeConfig()
    ctx.agent = None
    return ctx


def test_import_saves_endpoint_and_masks_everywhere(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home, findings = _scan(tmp_path, monkeypatch)
    mine = next(p for p in findings["opencode"].providers if p.name == "mine")
    message, refusal = ai.import_provider(BeeConfig(), None, mine, workdir=".")
    assert refusal == "", refusal
    back = json.loads(Path("beeagent.json").read_text(encoding="utf-8"))
    custom = {c["name"]: c for c in back["custom_providers"]}
    assert custom["mine"]["url"] == "https://mine.test/v1"
    assert custom["mine"]["key"] == LITERAL_KEY
    assert LITERAL_KEY not in message
    assert "…" in message


def test_import_of_a_preset_key_lands_on_the_preset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-env")
    home = _machine(tmp_path / 'home', monkeypatch)
    groq = next(p for f in ai.scan(home, home) for p in f.providers
                if p.name == "groq" and p.key_from == "env:GROQ_API_KEY")
    message, refusal = ai.import_provider(BeeConfig(), None, groq, workdir=".")
    assert refusal == "", refusal
    back = json.loads(Path("beeagent.json").read_text(encoding="utf-8"))
    assert back["api_keys"]["groq"] == "gsk-test-env"
    assert "gsk-test-env" not in message


def test_import_without_url_or_preset_is_refused_not_crashed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stray = ai.FoundProvider(agent="x", name="nowhere", url="", models=[])
    message, refusal = ai.import_provider(BeeConfig(), None, stray, workdir=".")
    assert message == "" and refusal, "an address-less import must be refused"


def test_preset_name_collision_gets_prefixed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = _machine(tmp_path / 'home', monkeypatch)
    clash = ai.FoundProvider(agent="opencode", name="groq",
                             url="https://other.test/v1", models=["m"],
                             key="k-123", key_from="file")
    message, refusal = ai.import_provider(BeeConfig(), None, clash, workdir=".")
    assert refusal == "", refusal
    back = json.loads(Path("beeagent.json").read_text(encoding="utf-8"))
    names = [c["name"] for c in back["custom_providers"]]
    assert names == ["opencode-groq"], names


def test_list_shows_tails_never_keys(tmp_path, monkeypatch):
    import io

    from rich.console import Console

    from beeagent.ui import commands as core

    monkeypatch.chdir(tmp_path)
    # The command scans the (faked) home, so build the machine there.
    _machine(Path.home(), monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-env")
    result = core._providers_import(_ctx(), [])
    buf = io.StringIO()
    Console(file=buf, width=120).print(result.output)
    text = buf.getvalue()
    assert "OpenCode" in text and "Codex CLI" in text
    assert LITERAL_KEY not in text and ENV_KEY not in text
    assert "…" in text


def test_import_by_number_and_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _machine(Path.home(), monkeypatch)
    result_ok = _import_call(["1"])
    assert "llm7" in result_ok.output.plain
    back = json.loads(Path("beeagent.json").read_text(encoding="utf-8"))
    assert back["custom_providers"], "the first numbered provider was saved"

    result_named = _import_call(["mine"])
    assert "mine" in result_named.output.plain

    result_missing = _import_call(["no-such-provider"])
    assert "nothing importable" in result_missing.output.plain


def _import_call(args):
    from beeagent.ui import commands as core

    return core._providers_import(_ctx(), args)
