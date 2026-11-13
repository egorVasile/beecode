# Releasing BeeCode

Every release is a tag; `CHANGELOG.md` says what rode it. Green CI on `main`
is the gate — never tag over red.

1. `python -m pytest tests -q` green locally (CI repeats it on Ubuntu + Windows).
2. Bump the version in all three files — `beeagent/__init__.py`,
   `pyproject.toml`, `package.json` (a test fails the build if they disagree).
3. `CHANGELOG.md`: one entry per shipped behavior, no marketing.
4. `git tag vX.Y.Z && git push && git push --tags`.
5. Smoke the exact user path in a fresh venv: create it, `pip install
   "git+https://github.com/egorVasile/beecode.git"`, `beecode --help` and one
   `/doctor` run. Never smoke in the dev checkout — it hides install bugs.
6. Never update or reinstall while any `beecode.exe` runs (see `cli._other_beecode_running`):
   pip uninstalls first and Windows locks the launcher back.

Known release debt (not forgotten, not yet scheduled):

- Public npm still carries `beecode@4.0.0` while the repo is past 10.x. Its
  launcher installs from git `main`, so it bootstraps current code — but the
  number lies, and `npm i -g beecode` should mean a release, not a branch.
  Decide: publish per tag, or retire the npm package.
