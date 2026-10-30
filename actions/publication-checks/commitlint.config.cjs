const githubRevertPattern = /^Revert "(?:build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)(?:\([^)]+\))?!?: [a-z](?:[^\n]*[^.\n])?"$/;
const isGithubRevert = (parsed) => githubRevertPattern.test(parsed.header);

module.exports = {
  extends: ["@commitlint/config-conventional"],
  defaultIgnores: false,
  plugins: [{
    rules: {
      "github-revert-or-present-type": (parsed) => [
        isGithubRevert(parsed) || Boolean(parsed.type),
        "type may not be empty",
      ],
      "github-revert-or-present-subject": (parsed) => [
        isGithubRevert(parsed) || Boolean(parsed.subject),
        "subject may not be empty",
      ],
      "subject-lowercase-start": (parsed) => [
        isGithubRevert(parsed) || /^[a-z]/.test(parsed.subject),
        "subject must start with a lowercase letter",
      ],
      "header-no-issue-number": (parsed) => [!/#\d+/.test(parsed.header), "pull request title must not contain an issue number"],
    },
  }],
  rules: {
    "type-empty": [0],
    "subject-empty": [0],
    "github-revert-or-present-type": [2, "always"],
    "github-revert-or-present-subject": [2, "always"],
    "type-enum": [2, "always", ["build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"]],
    "subject-case": [2, "never", ["sentence-case", "start-case", "pascal-case", "upper-case"]],
    "subject-lowercase-start": [2, "always"],
    "header-no-issue-number": process.env.COMMITLINT_CONTEXT === "pull-request-title" ? [2, "always"] : [0],
    "header-full-stop": [2, "never", "."],
  },
};
