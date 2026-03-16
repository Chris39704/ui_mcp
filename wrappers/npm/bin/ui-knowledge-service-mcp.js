#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

function findRepoRoot(startDir) {
  let current = path.resolve(startDir);

  while (true) {
    const pyproject = path.join(current, "pyproject.toml");
    const cliEntry = path.join(current, "src", "ui_knowledge_service", "cli.py");
    if (fs.existsSync(pyproject) && fs.existsSync(cliEntry)) {
      return current;
    }

    const parent = path.dirname(current);
    if (parent === current) {
      return null;
    }
    current = parent;
  }
}

function buildLaunchCommand() {
  const repoRoot = findRepoRoot(__dirname);
  const env = { ...process.env };

  if (repoRoot) {
    if (!env.UIKS_DATA_DIR) {
      env.UIKS_DATA_DIR = path.join(repoRoot, ".data", "ui_knowledge_service");
    }
    if (!env.UV_CACHE_DIR) {
      env.UV_CACHE_DIR = path.join(repoRoot, ".uv-cache");
    }
    return {
      command: "uv",
      args: ["run", "--directory", repoRoot, "--no-sync", "ui-knowledge-service-mcp"],
      env,
    };
  }

  return {
    command: "uvx",
    args: ["--from", "ui-knowledge-service", "ui-knowledge-service-mcp"],
    env,
  };
}

const launch = buildLaunchCommand();
const child = spawn(launch.command, launch.args, {
  stdio: "inherit",
  env: launch.env,
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});

child.on("error", (error) => {
  console.error(error.message);
  process.exit(1);
});
