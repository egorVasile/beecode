#!/usr/bin/env node
"use strict";

/*
 * BeeCode launcher for people who live in npm.
 *
 * BeeCode is a Python program, so this does not reimplement it: it creates one
 * private virtualenv under ~/.beecode, installs the published package into it,
 * and hands over. Nothing touches the system Python, and because the install
 * comes from the repository, `--update` is also how you get a fresh g4f.
 */

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SOURCE = process.env.BEECODE_SOURCE || "git+https://github.com/egorVasile/beecode.git";
const HOME = process.env.BEECODE_HOME || path.join(os.homedir(), ".beecode");
const VENV = path.join(HOME, "venv");
const VENV_PYTHON = process.platform === "win32"
  ? path.join(VENV, "Scripts", "python.exe")
  : path.join(VENV, "bin", "python");
const BEECODE = process.platform === "win32"
  ? path.join(VENV, "Scripts", "beecode.exe")
  : path.join(VENV, "bin", "beecode");

function run(command, args, { quiet = false } = {}) {
  const result = spawnSync(command, args, { stdio: quiet ? "ignore" : "inherit" });
  if (result.error) {
    console.error(`could not run ${command}: ${result.error.message}`);
    process.exit(1);
  }
  return result.status;
}

function findPython() {
  const probe = "import sys; sys.exit(sys.version_info < (3, 10))";
  for (const candidate of [process.env.BEECODE_PYTHON, "python", "python3"]) {
    if (!candidate) continue;
    const result = spawnSync(candidate, ["-c", probe], { stdio: "ignore" });
    if (!result.error && result.status === 0) return candidate;
  }
  console.error("BeeCode needs Python 3.10 or newer on PATH — get it from https://python.org");
  console.error('(on Windows, tick "Add python.exe to PATH" during the install)');
  process.exit(1);
}

function runningBeeCode() {
  // Windows locks the image of a running .exe: pip would uninstall the package
  // and then fail to write the launcher back, which leaves an install that
  // starts with "No module named 'beeagent'".
  if (process.platform !== "win32") return false;
  const probe = spawnSync("tasklist", ["/FO", "CSV", "/NH"], { encoding: "utf8" });
  return !probe.error && /"(beecode|beeagent)\.exe"/i.test(probe.stdout || "");
}

function install(python) {
  console.log(`installing BeeCode into ${VENV} — once, then every start is instant`);
  if (!fs.existsSync(VENV_PYTHON) && run(python, ["-m", "venv", VENV]) !== 0) process.exit(1);
  if (run(VENV_PYTHON, ["-m", "pip", "install", "--upgrade", "pip"], { quiet: true }) !== 0) process.exit(1);
  // The published version does not move between commits, so `--upgrade` alone
  // reports the installed 0.1.0 as already satisfied: pip refreshes g4f around
  // it and BeeCode itself stays on the old code. Replace it by force, then let
  // a second pass bring the dependencies forward.
  if (run(VENV_PYTHON, ["-m", "pip", "install", "--upgrade", "--force-reinstall",
                        "--no-deps", SOURCE]) !== 0) {
    console.error("the install failed — a network hiccup is usually worth retrying");
    process.exit(1);
  }
  if (run(VENV_PYTHON, ["-m", "pip", "install", "--upgrade", SOURCE], { quiet: true }) !== 0) {
    console.error("BeeCode updated, but its dependencies did not — run `beecode --update` again");
    process.exit(1);
  }
}

function main() {
  const args = process.argv.slice(2);
  const update = args.includes("--update");
  const rest = args.filter((arg) => arg !== "--update");

  const python = findPython();
  if (update) {
    if (runningBeeCode()) {
      console.error("BeeCode is running in another window, and pip cannot replace the "
                    + "files it is using — close it, then run `beecode --update` again.");
      process.exit(1);
    }
    install(python);
    // `--update` is a request to refresh, not to run: same contract as the CLI's.
    process.exit(0);
  }
  if (!fs.existsSync(BEECODE)) install(python);

  const result = spawnSync(BEECODE, rest, { stdio: "inherit" });
  if (result.error) {
    console.error(`could not start ${BEECODE}: ${result.error.message}`);
    process.exit(1);
  }
  process.exit(result.status === null ? 1 : result.status);
}

main();
