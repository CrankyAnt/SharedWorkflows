const { spawnSync } = require("node:child_process");
const [base, head] = process.argv.slice(2);
function git(...arguments_) {
  const result = spawnSync("git", arguments_, { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
  return result.stdout.trim();
}
let failed = false;
for (const commit of git("rev-list", "--reverse", `${base}..${head}`).split("\n").filter(Boolean)) {
  const parents = git("rev-list", "--parents", "-n", "1", commit).split(" ").length - 1;
  if (parents > 1) {
    console.log(`Skipping merge commit ${commit} with ${parents} parents.`);
    continue;
  }
  console.log(`Checking commit ${commit}.`);
  const result = spawnSync(process.execPath, [process.env.COMMITLINT_CLI, "--config", process.env.COMMITLINT_CONFIG, "--cwd", process.env.GITHUB_WORKSPACE, "--verbose"], {
    encoding: "utf8",
    input: git("log", "-1", "--format=%B", commit),
  });
  process.stdout.write(result.stdout ?? "");
  process.stderr.write(result.stderr ?? "");
  failed ||= result.status !== 0;
}
process.exitCode = failed ? 1 : 0;
