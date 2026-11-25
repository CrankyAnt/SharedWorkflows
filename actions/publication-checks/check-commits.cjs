const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");

const [base, head] = process.argv.slice(2);

function git(...arguments_) {
  const result = spawnSync("git", arguments_, { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || `git exited with status ${result.status}`);
  return result.stdout;
}

function resolveDefault(moduleNamespace) {
  return typeof moduleNamespace === "function" ? moduleNamespace : moduleNamespace.default;
}

async function loadLinter() {
  const cliPath = process.env.COMMITLINT_CLI;
  if (!cliPath || !fs.existsSync(cliPath)) {
    throw new Error(`commitlint CLI is missing or unset: ${cliPath || "(empty)"}`);
  }
  const toolRequire = createRequire(path.resolve(cliPath));
  const loadPath = toolRequire.resolve("@commitlint/load");
  const lintPath = toolRequire.resolve("@commitlint/lint");
  const formatPath = toolRequire.resolve("@commitlint/format");
  const [loadMod, lintMod, formatMod] = await Promise.all([
    import(pathToFileURL(loadPath).href),
    import(pathToFileURL(lintPath).href),
    import(pathToFileURL(formatPath).href),
  ]);
  const load = resolveDefault(loadMod);
  const lint = resolveDefault(lintMod);
  const format = resolveDefault(formatMod);
  if (typeof load !== "function" || typeof lint !== "function" || typeof format !== "function") {
    throw new Error("commitlint modules did not export load, lint, and format functions");
  }
  const configPath = process.env.COMMITLINT_CONFIG;
  const cwd = process.env.GITHUB_WORKSPACE || process.cwd();
  const config = await load({}, { file: configPath, cwd });
  return { config, lint, format };
}

async function main() {
  const { config, lint, format } = await loadLinter();
  const raw = git("log", "--reverse", "--format=%H%x1f%P%x1f%B%x1e", `${base}..${head}`);
  const parserOpts = config.parserPreset?.parserOpts;
  const lintOptions = {
    defaultIgnores: config.defaultIgnores,
    ignores: config.ignores,
    plugins: config.plugins,
    helpUrl: config.helpUrl,
    ...(parserOpts ? { parserOpts } : {}),
  };
  let failed = false;
  for (const record of raw.split("\x1e")) {
    if (!record) continue;
    const first = record.indexOf("\x1f");
    const second = record.indexOf("\x1f", first + 1);
    if (first === -1 || second === -1) continue;
    const commit = record.slice(0, first).trim();
    if (!commit) continue;
    const parentsText = record.slice(first + 1, second).trim();
    const parents = parentsText ? parentsText.split(/\s+/).length : 0;
    if (parents > 1) {
      console.log(`Skipping merge commit ${commit} with ${parents} parents.`);
      continue;
    }
    const message = record.slice(second + 1).trim();
    console.log(`Checking commit ${commit}.`);
    const report = await lint(message, config.rules, lintOptions);
    const output = format({ results: [report] }, { verbose: true, helpUrl: config.helpUrl });
    if (output) {
      process.stdout.write(output.endsWith("\n") ? output : `${output}\n`);
    }
    if (!report.valid) failed = true;
  }
  process.exitCode = failed ? 1 : 0;
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exitCode = 1;
});
