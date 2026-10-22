#!/usr/bin/env node
"use strict";
const { spawn } = require("node:child_process");
const path = require("node:path");
const child = spawn("python3", [path.join(__dirname, "../scripts/airdrop_triage.py"), ...process.argv.slice(2)], { stdio: "inherit" });
child.on("error", () => {
  console.error("[ERROR] OmaDrop triage requires Python 3.11 or newer on PATH.");
  process.exitCode = 1;
});
child.on("exit", (code, signal) => { process.exitCode = code ?? (signal === "SIGINT" ? 130 : 1); });
