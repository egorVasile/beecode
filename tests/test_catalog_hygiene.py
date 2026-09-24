"""Catalog hygiene: every claim we publish about an entry must be checkable.

`beeagent/plugins/data/catalog.json` is a public file that tells users two
things about each entry: which upstream project it comes from, and under which
licence that project ships. Getting either wrong is not a cosmetic bug - it is
us asserting something false about someone else's code, and it is exactly the
kind of claim nobody re-reads once it has shipped.

So the *shape* of those claims is pinned here: an entry that wants to be listed
has to say who made it, under what licence (from a set we know how to spell),
and has to say so in a form a person can click. The JSON is parsed straight
from disk rather than through Catalog, so this file keeps its teeth no matter
how the loader is refactored.
"""
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "beeagent" / "plugins" / "data" / "catalog.json"
TEMPLATES_DIR = REPO_ROOT / "beeagent" / "plugins" / "templates"

#: Our own repo: the only honest upstream for a `builtin` entry.
BEECODE_URL = "https://github.com/egorVasile/beecode"
BEECODE_LICENSE = "GPL-3.0-or-later"

#: The no-claim value. Used when upstream's own licence file cannot be reduced
#: to one SPDX id - it says "we did not decide", not "it is free to use".
NO_CLAIM = "NOASSERTION"

#: Licences we are willing to print for a user. Anything outside this set is a
#: failure, so a copy-paste of "Proprietary" or a typo'd id cannot ship.
ALLOWED_LICENSES = {
    "MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD",
    "MPL-2.0", "Unlicense", "Unlicense OR MIT", "MIT OR Unlicense", "MIT OR Apache-2.0",
    "CC0-1.0", "CC-BY-4.0", "BlueOak-1.0.0", "Python-2.0",
    "GPL-2.0-only", "GPL-3.0-only", "GPL-3.0-or-later", "LGPL-3.0-or-later",
    "AGPL-3.0-only", "MulanPSL-2.0", NO_CLAIM,
}

# https://github.com/<owner>/<repo> and nothing more: no /tree/branch, no
# trailing slash, no mirrored path - a URL we can only read in a browser is a
# claim nobody can check.
GITHUB_PROJECT_RE = re.compile(
    r"^https://github\.com/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,38})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TYPES = {"skill", "plugin", "mcp"}

#: Key-shaped strings must never ride along in a published catalog, neither in
#: a value nor in prose: the market index and the /plugins picker both echo this
#: file back to users, and a copy-pasted test key in a note becomes a leak.
SECRET_PATTERNS = [
    ("GitHub token", r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}"),
    ("OpenAI-style secret key", r"\bsk-[A-Za-z0-9_-]{16,}"),
    ("AWS access key id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{8,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{30,}"),
    ("Stripe key", r"\b[ps]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("npm token", r"\bnpm_[A-Za-z0-9]{30,}"),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("credential assignment",
     r"(?i)\b(?:api[_-]?key|apikey|access[_-]?token|secret|token|password|passwd|"
     r"credential)s?\b\"?\s*[:=]\s*\"?(?!\$)[^\"$\s<>,]{8,}"),
]


def raw_text() -> str:
    return CATALOG_PATH.read_text(encoding="utf-8")


def catalog() -> dict:
    return json.loads(raw_text())


def items() -> list:
    return catalog()["items"]


def test_the_file_the_whole_suite_is_about_exists():
    assert CATALOG_PATH.is_file(), CATALOG_PATH


def test_every_entry_names_itself_and_its_own_licence_and_upstream():
    for entry in items():
        label = entry.get("name") or entry.get("id") or "<unnamed>"
        for field in ("id", "name", "description", "license", "upstream"):
            assert isinstance(entry.get(field), str), f"{label}: {field} is missing"
            assert entry[field].strip(), f"{label}: {field} is blank"
        assert ID_RE.match(entry["id"]), f"{label}: id {entry['id']!r} is not a slug"
        assert GITHUB_PROJECT_RE.match(entry["upstream"]), \
            f"{label}: upstream {entry['upstream']!r} is not https://github.com/<owner>/<repo>"


def test_ids_are_unique_and_are_the_same_handle_as_names():
    """Two identities per entry is two chances to disagree about one."""
    seen = {}
    for entry in items():
        assert entry["id"] == entry["name"], \
            f"{entry['name']}: id {entry['id']!r} disagrees with the name"
        seen.setdefault(entry["id"], []).append(entry["name"])
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dupes, f"duplicate ids: {dupes}"


def test_no_entry_is_marked_with_a_licence_we_do_not_allow():
    for entry in items():
        assert entry["license"] in ALLOWED_LICENSES, \
            f"{entry['name']}: licence {entry['license']!r} is not in ALLOWED_LICENSES"


def test_a_licence_claim_we_could_not_pin_down_says_so_and_why():
    """NOASSERTION is honest only when it is explained; a blank note is a guess."""
    for entry in items():
        if entry["license"] != NO_CLAIM:
            continue
        note = entry.get("license_note", "")
        assert len(note) > 30, \
            f"{entry['name']}: a no-claim licence must carry a license_note explaining it"
        assert re.search(r"NOASSERTION|mixed|SEE LICENSE", note), \
            f"{entry['name']}: the note does not say why no licence is claimed"


def test_every_third_party_entry_records_where_the_claim_came_from():
    """An entry we did not read a page about is an entry nobody verified."""
    for entry in items():
        if entry["source"].get("kind") != "mcp":
            continue
        assert ISO_DATE_RE.match(entry.get("reviewed", "")), \
            f"{entry['name']}: reviewed must be an ISO date, not {entry.get('reviewed')!r}"
        assert len(entry.get("license_note", "")) > 30, \
            f"{entry['name']}: no license_note - the licence is a guess until it has a source"


def test_builtin_entries_only_claim_what_this_repository_actually_ships():
    for entry in items():
        if entry["source"].get("kind") != "builtin":
            continue
        assert entry["upstream"] == BEECODE_URL, \
            f"{entry['name']}: a builtin is ours, so its upstream is us"
        assert entry["license"] == BEECODE_LICENSE, \
            f"{entry['name']}: this repo is {BEECODE_LICENSE}, say that and nothing else"
        path = entry["source"].get("path")
        assert path, f"{entry['name']}: builtin source has no path"
        assert (TEMPLATES_DIR / path).is_dir(), \
            f"{entry['name']}: promises templates/{path}, which is not in the tree"


def test_third_party_entries_do_not_call_themselves_ours():
    for entry in items():
        if entry["source"].get("kind") == "mcp":
            assert entry["upstream"] != BEECODE_URL, \
                f"{entry['name']}: points at our repo but we do not write it"
            assert entry["license"] != BEECODE_LICENSE, \
                f"{entry['name']}: our licence is not the upstream's licence"


def test_mcp_entries_describe_a_launch_command_not_a_clone_target():
    """`source.url` is the git-clone field; a URL on an mcp entry means the
    manager would clone whatever a provenance note was trying to cite."""
    for entry in items():
        source = entry["source"]
        kind = source.get("kind")
        assert kind in ("builtin", "mcp", "git"), f"{entry['name']}: kind {kind!r}"
        assert entry["type"] in TYPES, f"{entry['name']}: type {entry['type']!r}"
        assert entry["category"] in catalog()["categories"], \
            f"{entry['name']}: category {entry['category']!r} is not a listed category"
        if kind == "mcp":
            assert "url" not in source, f"{entry['name']}: mcp entry carries a clone URL"
            assert isinstance(source.get("command"), str) and source["command"].strip(), \
                f"{entry['name']}: mcp entry has no command"
            assert all(isinstance(a, str) for a in source.get("args", [])), \
                f"{entry['name']}: args must be strings"
            for key, value in (source.get("env") or {}).items():
                assert value == "" or re.match(r"^(<.+>|\$\{.+\})$", str(value)), \
                    f"{entry['name']}: env.{key} looks like a real secret"


def test_nobody_invented_a_second_spelling_of_the_licence_field():
    for entry in items():
        odd = [k for k in entry
               if re.match(r"(?i)^licen[cs]e", k) and k not in ("license", "license_note")]
        assert not odd, f"{entry['name']}: unexpected licence field(s) {odd}"


def test_an_entry_flagged_archived_says_who_archived_it():
    for entry in items():
        if not entry.get("archived"):
            continue
        assert entry["archived"] is True, f"{entry['name']}: archived must be true"
        assert "archiv" in entry.get("license_note", "").lower(), \
            f"{entry['name']}: flagged archived with no note pointing at the archive"


def test_the_raw_text_carries_nothing_that_looks_like_a_key():
    """Run on the file, not the parsed values: a token hidden in prose in a
    license_note is just as public as one in an env dict."""
    text = raw_text()
    for name, pattern in SECRET_PATTERNS:
        hit = re.search(pattern, text)
        assert hit is None, f"{name} shape found in catalog.json: {hit.group(0)[:24]!r}..."
