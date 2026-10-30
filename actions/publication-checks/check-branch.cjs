const [branch] = process.argv.slice(2);
const { verifySpecification } = require("./verify-spec.cjs");
const specification = verifySpecification();
const RESEARCH_BRANCH = /^research\/[a-z0-9]+(?:-[a-z0-9]+)*$/;
const AGENT_BRANCH = /^(?:ai|copilot|cursor|claude|codex)\//;
const STANDARD_BRANCH = /^(?:feat|fix|hotfix|chore|release)\/[a-z0-9]+(?:-[a-z0-9]+)*$/;
const RELEASE_BRANCH = /^release\/v[0-9]+(?:\.[0-9]+)*(?:-[a-z0-9]+(?:[.-][a-z0-9]+)*)?$/;
const GITHUB_REVERT_BRANCH = /^revert-[1-9][0-9]*-.+$/;
function checkBranch(branch) {
  if (AGENT_BRANCH.test(branch)) throw new Error("agent branch prefixes are not allowed");
  if (RESEARCH_BRANCH.test(branch)) return;
  if (GITHUB_REVERT_BRANCH.test(branch)) return;
  if (!new RegExp(specification.grammar.regex).test(branch)) throw new Error("branch does not match the recorded Conventional Branch specification");
  if (!STANDARD_BRANCH.test(branch) && !RELEASE_BRANCH.test(branch)) throw new Error("branch does not match the repository branch policy");
}
try {
  checkBranch(branch ?? "");
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
