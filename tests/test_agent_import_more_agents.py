"""New detectors added on top of the previous agent's `agent_import.py` work:
Zed (`language_models.openai_compatible`), DeepSeek Harness (`settings.yaml`
under `llm-pi-ai.providers`), and a real `state.vscdb` read for Cline/Roo/Kilo
instead of the old presence-only stub.

Same rules as `test_agent_import.py`: nothing is written outside `tmp_path`,
a key never appears in a message, and a file that does not parse is absence.
"""
import json
import sqlite3
from pathlib import Path

from beeagent.core import agent_import as ai


def test_zed_reads_openai_compatible_endpoint_and_env_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    zed = home / ".config" / "zed"
    zed.mkdir(parents=True)
    (zed / "settings.json").write_text(json.dumps({
        "language_models": {
            "openai_compatible": {
                "My Gateway": {
                    "api_url": "https://gw.test/v1",
                    "available_models": [{"name": "big-model"}, {"name": "small-model"}],
                }
            }
        }
    }), encoding="utf-8")
    monkeypatch.setenv("MY_GATEWAY_API_KEY", "sk-test-zed-1234")
    found = {f.id: f for f in ai.scan(home, home)}["zed"]
    assert found.found
    provider = found.providers[0]
    assert provider.url == "https://gw.test/v1"
    assert provider.key == "sk-test-zed-1234"
    assert provider.key_from == "env:MY_GATEWAY_API_KEY"
    assert "big-model" in provider.models


def test_zed_without_env_key_reports_absence_not_a_guess(tmp_path, monkeypatch):
    home = tmp_path / "home"
    zed = home / ".config" / "zed"
    zed.mkdir(parents=True)
    (zed / "settings.json").write_text(json.dumps({
        "language_models": {"openai_compatible": {"nokeyhere": {"api_url": "https://x.test/v1"}}}
    }), encoding="utf-8")
    monkeypatch.delenv("NOKEYHERE_API_KEY", raising=False)
    found = {f.id: f for f in ai.scan(home, home)}["zed"]
    provider = found.providers[0]
    assert provider.key is None
    assert "NOKEYHERE_API_KEY" in provider.note


def test_dsh_reads_providers_and_model_ids(tmp_path, monkeypatch):
    home = tmp_path / "home"
    dsh = home / ".dsh"
    dsh.mkdir(parents=True)
    (dsh / "settings.yaml").write_text(
        "llm-pi-ai:\n"
        "  providers:\n"
        "    atlas:\n"
        "      displayName: Atlas Cloud\n"
        "      baseURL: https://api.atlascloud.ai/v1\n"
        "      apiKeyEnv: ATLASCLOUD_API_KEY\n"
        "      models:\n"
        "        - id: deepseek-ai/deepseek-v4-flash\n"
        "        - id: deepseek-ai/deepseek-v4-pro\n",
        encoding="utf-8")
    monkeypatch.setenv("ATLASCLOUD_API_KEY", "sk-test-dsh-9999")
    found = {f.id: f for f in ai.scan(home, home)}["dsh"]
    assert found.found
    provider = found.providers[0]
    assert provider.name == "atlas"
    assert provider.url == "https://api.atlascloud.ai/v1"
    assert provider.key == "sk-test-dsh-9999"
    assert provider.key_from == "env:ATLASCLOUD_API_KEY"
    assert provider.models == ["deepseek-ai/deepseek-v4-flash", "deepseek-ai/deepseek-v4-pro"]


def test_dsh_missing_env_key_is_noted_not_fabricated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    dsh = home / ".dsh"
    dsh.mkdir(parents=True)
    (dsh / "settings.yaml").write_text(
        "llm-pi-ai:\n  providers:\n    atlas:\n      baseURL: https://x.test/v1\n"
        "      apiKeyEnv: MISSING_HERE\n", encoding="utf-8")
    monkeypatch.delenv("MISSING_HERE", raising=False)
    found = {f.id: f for f in ai.scan(home, home)}["dsh"]
    provider = found.providers[0]
    assert provider.key is None
    assert ".credentials.yaml" in provider.note


def _fake_vscdb(path: Path, rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    for key, value in rows.items():
        conn.execute("INSERT INTO ItemTable VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def test_vscode_reads_url_and_literal_key_from_state_vscdb(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ext = home / ".vscode" / "extensions" / "kilocode.kilo-code-4.2.0"
    ext.mkdir(parents=True)
    state = (home / ".config" / "Code" / "User" / "globalStorage" /
             "kilocode.kilo-code" / "state.vscdb")
    _fake_vscdb(state, {
        "kilo-code.config": json.dumps({
            "providerProfiles": {
                "apiConfigs": {
                    "default": {"openAiBaseUrl": "https://kilo.test/v1",
                                "openAiApiKey": "sk-test-kilo-7777"}
                }
            }
        }),
    })
    found = {f.id: f for f in ai.scan(home, home)}["vscode"]
    assert found.found
    kilo = next(p for p in found.providers if p.agent == "vscode" and
               "kilo.test" in (p.url or ""))
    assert kilo.url == "https://kilo.test/v1"
    assert kilo.key == "sk-test-kilo-7777"
    assert kilo.key_from == "file"


def test_vscode_extension_present_but_nothing_readable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ext = home / ".vscode" / "extensions" / "rooveterinaryinc.roo-cline-1.0.0"
    ext.mkdir(parents=True)
    found = {f.id: f for f in ai.scan(home, home)}["vscode"]
    assert found.found
    roo = next(p for p in found.providers if p.agent == "vscode")
    assert roo.key is None
    assert roo.url == ""
    assert "secret storage" in roo.note


def test_vscode_no_extension_anywhere_is_reported_absent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    found = {f.id: f for f in ai.scan(home, home)}["vscode"]
    assert not found.found
    assert found.providers == []


def test_broken_zed_and_dsh_files_are_absence_not_errors(tmp_path, monkeypatch):
    home = tmp_path / "home"
    zed = home / ".config" / "zed"
    zed.mkdir(parents=True)
    (zed / "settings.json").write_text("{not json", encoding="utf-8")
    dsh = home / ".dsh"
    dsh.mkdir(parents=True)
    (dsh / "settings.yaml").write_text("not: [valid, yaml, {{{", encoding="utf-8")
    findings = {f.id: f for f in ai.scan(home, home)}
    assert findings["zed"].found is False
    assert findings["dsh"].found is True          # the file parsed to *something*
    assert findings["dsh"].providers == []
