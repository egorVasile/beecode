#!/usr/bin/env node
"use strict";

/*
 * BeeCode launcher for people who live in npm.
 *
 * BeeCode is a Python program, so this does not reimplement it: it creates one
 * private virtualenv under ~/.beecode, installs the published package into it,
 * and hands over. Nothing touches the system Python, and because the install
 * comes from the repository, `--update` is also how you get a fresh g4f.
 *
 * On Termux the manifest cannot say what a phone needs — a dependency list has
 * no `--no-deps` — so `--keyless` (and every install there) adds g4f by hand, in
 * the one order that needs no compiler.
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

// Node on Termux reports "android"; PREFIX is what Termux itself sets. Either
// way the box has no compiler and no apt, so the advice has to be `pkg install`.
const TERMUX = process.platform === "android"
  || (process.env.PREFIX || "").includes("com.termux");

// The keyless provider on a phone. g4f is pure Python — a `py3-none-any` wheel —
// and it is two of the five packages it declares that a phone cannot install:
// pycryptodome and brotli, which ship no Android wheel and have no pure-Python
// fallback, and neither of which BeeCode imports. `--no-deps` is what keeps them
// out; everything BeeCode's own path touches then arrives as a wheel that needs
// no compiler. aiohttp is the exception: instead of a pure-Python wheel it
// publishes prebuilt Android wheels carrying its C parser, and Termux's Python
// 3.14 has a reported crash in it — so it comes from the sdist, which is the
// only place AIOHTTP_NO_EXTENSIONS is read. The two belong together.
const KEYLESS_ENV = { AIOHTTP_NO_EXTENSIONS: "1" };
const KEYLESS_PIP = [
  ["install", "--no-deps", "g4f"],
  ["install", "requests", "nest-asyncio2"],
  ["install", "--no-binary=aiohttp", "aiohttp"],
];

function run(command, args, { quiet = false, env = null } = {}) {
  const options = { stdio: quiet ? "ignore" : "inherit" };
  if (env) options.env = Object.assign({}, process.env, env);
  const result = spawnSync(command, args, options);
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
  if (TERMUX) {
    console.error("on Termux nothing is on PATH until you put it there:");
    console.error("  pkg install python python-pip git");
    process.exit(1);
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

function ensureVenv(python) {
  if (!fs.existsSync(VENV_PYTHON) && run(python, ["-m", "venv", VENV]) !== 0) process.exit(1);
}

function keylessInstalled(python) {
  // find_spec, not import: this asks whether g4f is in the venv at all, and
  // making it answer on a phone costs seconds every run. The agent itself uses
  // the same question to decide whether to fall back to the pool.
  const probe = "import importlib.util, sys; sys.exit(importlib.util.find_spec('g4f') is None)";
  const result = spawnSync(python, ["-c", probe], { stdio: "ignore" });
  return !result.error && result.status === 0;
}

function addKeylessProvider(python) {
  console.log("adding the keyless provider — g4f without the two parts a phone "
              + "cannot install, which BeeCode never imports");
  for (const step of KEYLESS_PIP) {
    // The aiohttp step builds from an sdist and sits doing nothing visible for a
    // minute; there pip's own progress is the reassurance.
    const quiet = !step.includes("--no-binary=aiohttp");
    if (run(python, ["-m", "pip", ...step], { quiet, env: KEYLESS_ENV }) !== 0) {
      console.error("the keyless provider did not go in. BeeCode still works — the pool "
                    + "or your own key answer, and `--update` retries this — but here is "
                    + "the whole recipe, for typing it by hand:");
      console.error("  export AIOHTTP_NO_EXTENSIONS=1");
      for (const step2 of KEYLESS_PIP) {
        console.error(`  ${python} -m pip ${step2.join(" ")}`);
      }
      console.error("  (no clang in it: none of this compiles)");
      return;
    }
  }
  if (!keylessInstalled(python)) {
    console.error("pip was happy but g4f is not in the venv — /doctor says what it sees");
    return;
  }
  console.log("keyless provider in place: beecode --provider g4f");
  console.log("optional: pkg install python-brotli brings back the brotli codec, "
              + "pkg install tur-repo python-tiktoken the exact token count");
}

function offerKeylessProvider(python) {
  // A desktop gets g4f from the manifest; only here does the name have to be
  // coaxed in past two declarations that cannot build.
  if (!TERMUX || keylessInstalled(python)) return;
  addKeylessProvider(python);
}

function install(python) {
  console.log(`installing BeeCode into ${VENV} — once, then every start is instant`);
  ensureVenv(python);
  if (run(VENV_PYTHON, ["-m", "pip", "install", "--upgrade", "pip"], { quiet: true }) !== 0) process.exit(1);
  // The published version does not move between commits, so `--upgrade` alone
  // reports the installed 0.1.0 as already satisfied: pip refreshes g4f around
  // it and BeeCode itself stays on the old code. Replace it by force, then let
  // a second pass bring the dependencies forward.
  if (run(VENV_PYTHON, ["-m", "pip", "install", "--upgrade", "--force-reinstall",
                        "--no-deps", SOURCE]) !== 0) {
    console.error("the install failed — a network hiccup is usually worth retrying");
    if (TERMUX) console.error("on Termux the usual cause is a missing git: pkg install git");
    process.exit(1);
  }
  if (run(VENV_PYTHON, ["-m", "pip", "install", "--upgrade", SOURCE], { quiet: true }) !== 0) {
    console.error("BeeCode updated, but its dependencies did not — run `beecode --update` again");
    process.exit(1);
  }
  // Termux gets the name from the recipe above; a desktop takes it with the
  // manifest, and this returns without a word.
  offerKeylessProvider(VENV_PYTHON);
}

function main() {
  const args = process.argv.slice(2);
  const update = args.includes("--update");
  const keyless = args.includes("--keyless");
  const rest = args.filter((arg) => arg !== "--update" && arg !== "--keyless");

  const python = findPython();
  if (keyless) {
    // The one thing the manifest leaves out on a phone, on its own, without
    // touching the BeeCode that is already installed.
    if (!TERMUX) {
      console.log("this machine gets g4f with the install itself — nothing to add");
      process.exit(0);
    }
    ensureVenv(python);
    offerKeylessProvider(VENV_PYTHON);
    process.exit(0);
  }
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
