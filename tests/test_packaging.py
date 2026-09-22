"""The install itself, on every platform BeeCode claims.

A dependency is a promise about the machine: `pip install beecode` has to work on
a phone as well as a laptop, and on a phone there is no compiler and no wheel for
a Rust package. These read the real pyproject so the claim cannot drift from the
file.
"""
import pytest

tomllib = pytest.importorskip("tomllib")

from pathlib import Path

# Marker evaluation lives in `packaging`, which pip installs alongside itself.
Marker = pytest.importorskip("packaging.markers").Marker

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# What a phone reports: CPython sets sys.platform to "android" on Android, and
# Termux has no `pkg install python-pydantic` — there is no wheel for its Rust
# core, because musllinux wheels are Alpine's, not bionic's.
ANDROID = {"sys_platform": "android", "platform_system": "Android", "os_name": "posix",
           "platform_machine": "aarch64", "platform_release": "", "platform_version": "",
           "python_version": "3.12", "implementation_name": "cpython",
           "implementation_version": "3.12.0"}

DESKTOP = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
           "platform_machine": "x86_64", "platform_release": "", "platform_version": "",
           "python_version": "3.12", "implementation_name": "cpython",
           "implementation_version": "3.12.0"}

def dependencies_for(environment: dict) -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    kept = []
    for requirement in data["project"]["dependencies"]:
        name, _, marker = requirement.partition(";")
        if not marker or Marker(marker.strip()).evaluate(environment):
            kept.append(name.strip())
    return sorted(kept)


def test_a_phone_gets_an_install_that_needs_no_compiler():
    """Only pure-Python packages may be hard dependencies: everything else needs a
    wheel PyPI does not publish for Android, or a compiler Termux does not ship."""
    assert dependencies_for(ANDROID) == ["httpx>=0.25", "prompt_toolkit>=3.0",
                                         "rich>=13.0", "textual>=0.60"]


def test_a_laptop_still_gets_the_keyless_provider_and_the_tokenizer():
    """The markers must not quietly drop the two packages that are the product's
    headline on the platforms where they install fine."""
    on_desktop = dependencies_for(DESKTOP)
    assert "g4f>=0.3.0" in on_desktop
    assert "tiktoken" in on_desktop


def test_the_config_needs_no_third_party_validation():
    """pydantic was a hard dependency for one file of field defaults. BeeCode owns
    that now, so nothing has to import it — on any platform."""
    import subprocess
    import sys

    probe = (
        "import sys; sys.modules['pydantic'] = None;"
        "from beeagent.config.loader import load_config; from beeagent.config.schema import BeeConfig;"
        "print(BeeConfig().model_dump()['model'])"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            cwd=str(PYPROJECT.parent))
    assert result.returncode == 0, result.stderr
    assert "command-a-03-2025" in result.stdout
