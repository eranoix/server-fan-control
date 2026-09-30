#!/usr/bin/env node
// Commit message linter with no dependencies.
//
//   node scripts/commitlint.mjs --file .git/COMMIT_EDITMSG
//   node scripts/commitlint.mjs --range origin/main..HEAD
//   node scripts/commitlint.mjs --range HEAD        (every ancestor of HEAD)
//   node scripts/commitlint.mjs --text "fix(api): reject empty bodies"
//
// Allowed scopes live in .github/commit-scopes.txt, one per line.

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const TYPES = ["feat", "fix", "perf", "refactor", "style", "docs", "test", "build", "ci", "chore", "revert"];
const SQUASH_SUFFIX = / \(#\d+\)$/;
const MAX_SUBJECT = 72;
const MAX_BODY_LINE = 72;
const SUBJECT = new RegExp(`^(${TYPES.join("|")})(?:\\(([^)]*)\\))?: (.+?)(?: \\(([A-Z][A-Z0-9]*-\\d+)\\))?$`);
const FORBIDDEN = [
  [/^co-authored-by:/im, "no Co-Authored-By trailers"],
  [/\bclaude code\b/i, "no tool attribution"],
  [/\bgenerated (?:with|by)\b/i, "no tool attribution"],
];

function repoRoot() {
  try {
    return execFileSync("git", ["rev-parse", "--show-toplevel"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    return join(dirname(fileURLToPath(import.meta.url)), "..");
  }
}
const root = repoRoot();

function loadScopes() {
  const file = join(root, ".github", "commit-scopes.txt");
  if (!existsSync(file)) return null;
  return readFileSync(file, "utf8")
    .split("\n")
    .map((l) => l.replace(/#.*/, "").trim())
    .filter(Boolean);
}

function clean(raw) {
  const lines = [];
  for (const line of raw.replace(/\r\n/g, "\n").split("\n")) {
    if (/^# -+ >8 -+/.test(line)) break;
    if (line.startsWith("#")) continue;
    lines.push(line);
  }
  while (lines.length && lines[lines.length - 1].trim() === "") lines.pop();
  while (lines.length && lines[0].trim() === "") lines.shift();
  return lines;
}

export function lint(raw, scopes = loadScopes()) {
  const errors = [];
  const lines = clean(raw);
  if (!lines.length) return ["empty message"];
  // A squash merge on GitHub appends " (#N)" to the PR title. The title was
  // already checked on the pull request, so the suffix does not count here.
  const subject = lines[0].replace(SQUASH_SUFFIX, "");

  if (subject.length > MAX_SUBJECT) errors.push(`subject is ${subject.length} chars, max ${MAX_SUBJECT}`);
  if (/[^\x20-\x7e]/.test(subject)) errors.push("subject must be plain ASCII (no accents, no dashes like —)");

  const m = SUBJECT.exec(subject);
  if (!m) {
    errors.push(`subject must be "<type>(<scope>): <summary>", type one of: ${TYPES.join(", ")}`);
  } else {
    const [, , scope, summary] = m;
    if (scope !== undefined) {
      if (!/^[a-z][a-z0-9]*$/.test(scope)) errors.push(`scope "${scope}" must be one lowercase word`);
      else if (scopes && !scopes.includes(scope)) errors.push(`scope "${scope}" is not in .github/commit-scopes.txt`);
    }
    if (!/^[a-z0-9`'"]/.test(summary)) errors.push("summary must start with a lowercase letter");
    if (/[.!]$/.test(summary)) errors.push("summary must not end with punctuation");
    if (/^(added|adds|fixed|fixes|updated|updates|removed|removes|changed|changes)\b/i.test(summary))
      errors.push("summary must be imperative (add, fix, update, not added/adds)");
  }

  if (lines.length > 1 && lines[1].trim() !== "") errors.push("leave a blank line between subject and body");
  const body = lines.slice(2);
  body.forEach((line, i) => {
    if (line.length > MAX_BODY_LINE && !/:\/\/|^\s{4}|^\|/.test(line))
      errors.push(`body line ${i + 1} is ${line.length} chars, wrap at ${MAX_BODY_LINE}`);
    if (/^Release:/.test(line)) {
      if (i !== 0) errors.push("Release: must be the first line of the body");
      if (!/^Release: \d+\.\d+\.\d+$/.test(line)) errors.push('Release line must be "Release: X.Y.Z"');
    }
  });

  for (const [re, why] of FORBIDDEN) if (re.test(lines.join("\n"))) errors.push(why);
  return errors;
}

function commitsIn(range) {
  const out = execFileSync("git", ["log", "--format=%H%x00%B%x1e", range], { cwd: root, encoding: "utf8", maxBuffer: 256 << 20 });
  return out
    .split("\x1e")
    .map((c) => c.replace(/^\n/, ""))
    .filter(Boolean)
    .map((c) => {
      const i = c.indexOf("\x00");
      return { sha: c.slice(0, i), msg: c.slice(i + 1) };
    });
}

function main(argv) {
  const [flag, value] = argv;
  let items;
  if (flag === "--file" && value) items = [{ sha: value, msg: readFileSync(value, "utf8") }];
  else if (flag === "--text" && value !== undefined) items = [{ sha: "text", msg: value }];
  else if (flag === "--range" && value) items = commitsIn(value);
  else {
    console.error("usage: commitlint.mjs --file <path> | --range <a..b> | --text <message>");
    return 2;
  }

  const scopes = loadScopes();
  let bad = 0;
  for (const { sha, msg } of items) {
    const errors = lint(msg, scopes);
    if (!errors.length) continue;
    bad++;
    console.error(`\n${sha.slice(0, 12)}  ${clean(msg)[0] ?? ""}`);
    for (const e of errors) console.error(`  - ${e}`);
  }
  if (bad) {
    console.error(`\n${bad} of ${items.length} message(s) break the commit convention (see CONTRIBUTING.md).`);
    return 1;
  }
  if (flag === "--range") console.log(`${items.length} commit message(s) OK`);
  return 0;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) process.exit(main(process.argv.slice(2)));
