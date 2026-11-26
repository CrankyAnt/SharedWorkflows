const { spawnSync } = require("node:child_process");
const fs = require("node:fs");

const PAGE_SIZE = 100;
const PAGE_LIMIT = 100;
const TIMEOUT_MILLISECONDS = 30000;
// A page of the list answered with a rate-limit status is requested again after each backoff, so at most three times
// in all.
const RETRIED_STATUSES = new Set([403, 429]);
const BACKOFF_SECONDS = [1, 2];
const RETRY_WAIT_LIMIT_SECONDS = 60;
const MESSAGE_LIMIT = 300;
const RATE_LIMIT_HEADERS = ["x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-used", "x-ratelimit-reset", "x-ratelimit-resource", "retry-after"];

class UnreadableList extends Error {}
class UnreadablePullRequest extends Error {}

function git(...arguments_) {
  const result = spawnSync("git", arguments_, { encoding: "utf8", maxBuffer: 1024 * 1024 * 1024 });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || `git ${arguments_[0]} failed`);
  return result.stdout;
}

function pullRequestEvent() {
  let event;
  try {
    event = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH ?? "", "utf8"));
  } catch {
    throw new Error("the pull request event cannot be read from GITHUB_EVENT_PATH");
  }
  if (!event || typeof event.pull_request !== "object" || event.pull_request === null) {
    throw new Error("the pull request event in GITHUB_EVENT_PATH names no pull request");
  }
  return event;
}

// The API address and the headers of every request, with the job's token when there is one.
function apiRequest() {
  const api = (process.env.GITHUB_API_URL || "https://api.github.com").replace(/\/+$/, "");
  const headers = { Accept: "application/vnd.github+json", "User-Agent": "publication-checks" };
  if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  return { api, headers };
}

// The pull request as it is now, read with the job's token: a run that is started again after a corrected title or
// body checks the corrected text, which the event file of the first run does not hold.
async function currentPullRequest(event) {
  const repository = process.env.GITHUB_REPOSITORY ?? "";
  if (!/^[^/\s]+\/[^/\s]+$/.test(repository)) throw new Error("GITHUB_REPOSITORY names no repository");
  const number = event.pull_request.number ?? event.number;
  if (!Number.isInteger(number)) throw new Error("the pull request event in GITHUB_EVENT_PATH names no pull request number");
  const { api, headers } = apiRequest();
  let response;
  let pullRequest;
  try {
    response = await fetch(`${api}/repos/${repository}/pulls/${number}`, { headers, signal: AbortSignal.timeout(TIMEOUT_MILLISECONDS) });
  } catch (error) {
    throw new UnreadablePullRequest(`request failed (${error.cause?.code ?? error.name})`);
  }
  if (!response.ok) throw new UnreadablePullRequest(`HTTP ${response.status}`);
  try {
    pullRequest = await response.json();
  } catch {
    pullRequest = undefined;
  }
  const { title, body } = pullRequest ?? {};
  if (typeof title !== "string" || (typeof body !== "string" && body !== null && body !== undefined)) {
    throw new UnreadablePullRequest("the answer is not the expected JSON pull request");
  }
  return { title, body };
}

// Every text the check reads, with the name its positions are reported under.
function commitTexts(base, head) {
  const result = [];
  for (const commit of git("rev-list", "--reverse", `${base}..${head}`).split("\n").filter(Boolean)) {
    result.push({ place: `commit ${commit}`, text: git("log", "-1", "--format=%B", commit) });
  }
  return result;
}

// The repository names a reference may stand for: the name as written, and without the full stops that end a
// sentence and the `.git` of a clone address.
function candidates(name) {
  const trimmed = name.replace(/\.+$/, "").replace(/\.git$/i, "");
  return [...new Set([name, trimmed])].filter((candidate) => /[^.]/.test(candidate)).map((candidate) => candidate.toLowerCase());
}

function references(owner, sources) {
  const escaped = owner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const result = [];
  for (const { place, text } of sources) {
    text.split(/\r?\n/).forEach((line, index) => {
      for (const match of line.matchAll(new RegExp(`(?<![A-Za-z0-9-])${escaped}/([A-Za-z0-9._-]+)`, "gi"))) {
        const names = candidates(match[1]);
        if (names.length > 0) result.push({ place, line: index + 1, column: match.index + 1, names });
      }
    });
  }
  return result;
}

// Why a refused answer was refused: its status, GitHub's message and the rate-limit headers it carried. The message
// is GitHub's own text about the request, never a text that the check reads.
async function refusalReason(response) {
  const details = [];
  let message;
  try {
    message = JSON.parse(await response.text())?.message;
  } catch {
    message = undefined;
  }
  if (typeof message === "string" && message.trim()) details.push(message.replace(/\s+/g, " ").trim().slice(0, MESSAGE_LIMIT));
  for (const name of RATE_LIMIT_HEADERS) {
    const value = response.headers.get(name);
    if (value !== null) details.push(`${name}: ${value}`);
  }
  return details.length === 0 ? `HTTP ${response.status}` : `HTTP ${response.status} (${details.join("; ")})`;
}

// The seconds to wait before a refused page is requested again, as GitHub advises: `retry-after` when present,
// otherwise until `x-ratelimit-reset` when no request remains, otherwise a short fixed backoff.
function retryWait(response, attempt) {
  const retryAfter = secondsHeader(response, "retry-after");
  if (retryAfter !== undefined) return retryAfter;
  const reset = secondsHeader(response, "x-ratelimit-reset");
  if (response.headers.get("x-ratelimit-remaining")?.trim() === "0" && reset !== undefined) {
    return Math.max(0, reset - Date.now() / 1000);
  }
  return BACKOFF_SECONDS[attempt];
}

// A header that states a number of seconds, or undefined when it is absent or states something else.
function secondsHeader(response, name) {
  const value = response.headers.get(name);
  return value !== null && /^\s*\d+(\.\d+)?\s*$/.test(value) ? Number(value) : undefined;
}

// The owner's public repositories. The request carries the job's token when there is one, which GitHub limits per
// repository rather than per runner address; a login never widens the list, because only an entry that states it is
// not private, with a public visibility or none, counts as public.
async function publicRepositories(owner) {
  const { api, headers } = apiRequest();
  const names = new Set();
  for (let page = 1; page <= PAGE_LIMIT; page += 1) {
    let response;
    let listed;
    for (let attempt = 0; ; attempt += 1) {
      try {
        response = await fetch(`${api}/users/${encodeURIComponent(owner)}/repos?type=owner&per_page=${PAGE_SIZE}&page=${page}`, {
          headers,
          signal: AbortSignal.timeout(TIMEOUT_MILLISECONDS),
        });
      } catch (error) {
        throw new UnreadableList(`request failed (${error.cause?.code ?? error.name})`);
      }
      if (response.ok) break;
      if (!RETRIED_STATUSES.has(response.status) || attempt >= BACKOFF_SECONDS.length) throw new UnreadableList(await refusalReason(response));
      const wait = retryWait(response, attempt);
      if (wait > RETRY_WAIT_LIMIT_SECONDS) throw new UnreadableList(await refusalReason(response));
      await response.arrayBuffer().catch(() => undefined);
      await new Promise((resolve) => setTimeout(resolve, wait * 1000));
    }
    try {
      listed = await response.json();
    } catch {
      listed = undefined;
    }
    if (!Array.isArray(listed) || listed.some((repository) => typeof repository?.name !== "string")) {
      throw new UnreadableList("the answer is not the expected JSON list");
    }
    for (const repository of listed) {
      if (repository.private === false && (repository.visibility === undefined || repository.visibility === "public")) {
        names.add(repository.name.toLowerCase());
      }
    }
    if (listed.length < PAGE_SIZE) return names;
  }
  throw new UnreadableList(`the list has more than ${PAGE_LIMIT} pages`);
}

async function main() {
  const args = process.argv.slice(2);
  const mode = args[0]?.startsWith("--") ? args.shift() : undefined;
  const [base, head] = args;
  const owner = process.env.GITHUB_REPOSITORY_OWNER ?? "";
  if (!owner) throw new Error("GITHUB_REPOSITORY_OWNER names no owner");
  const event = pullRequestEvent();
  // Only a repository that states it is private is skipped: its commits, titles and bodies are not public.
  if (event.repository?.private === true) {
    console.log("Skipping the reference check: this repository is private, so its texts are not public.");
    return 0;
  }
  const sources = mode === "--pull-request-text" ? [] : commitTexts(base, head);
  if (mode !== "--commits") {
    try {
      const { title, body } = await currentPullRequest(event);
      sources.push({ place: "pull request title", text: title }, { place: "pull request body", text: body ?? "" });
    } catch (error) {
      if (!(error instanceof UnreadablePullRequest)) throw error;
      console.error(`the pull request's current title and body cannot be read: ${error.message}. The check cannot read the texts it checks; run it again.`);
      return 1;
    }
  }
  const found = references(owner, sources);
  if (found.length === 0) {
    console.log("No reference to a repository of the owner.");
    return 0;
  }
  let published;
  try {
    published = await publicRepositories(owner);
  } catch (error) {
    if (!(error instanceof UnreadableList)) throw error;
    console.error(`the list of public repositories of the owner cannot be read: ${error.message}. The check cannot tell whether the references are public; run it again.`);
    return 1;
  }
  const offending = found.filter(({ names }) => !names.some((name) => published.has(name)));
  for (const { place, line, column } of offending) {
    console.error(`${place}, line ${line}, column ${column}: reference to a repository of the owner that is not public`);
  }
  if (offending.length === 0) console.log(`Every reference to a repository of the owner names a public repository (${found.length}).`);
  return offending.length === 0 ? 0 : 1;
}

main().then(
  (status) => {
    process.exitCode = status;
  },
  (error) => {
    console.error(error.message);
    process.exitCode = 1;
  },
);
