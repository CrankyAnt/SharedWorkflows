const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
function verifySpecification(directory = __dirname) {
  const raw = fs.readFileSync(path.join(directory, "spec.json"));
  const metadata = JSON.parse(fs.readFileSync(path.join(directory, "spec-metadata.json"), "utf8"));
  const digest = crypto.createHash("sha256").update(raw).digest("hex");
  if (digest !== metadata.sha256) throw new Error("Conventional Branch specification hash mismatch");
  const specification = JSON.parse(raw);
  if (specification.version !== metadata.version || typeof specification.grammar?.regex !== "string") {
    throw new Error("invalid Conventional Branch specification version or grammar");
  }
  new RegExp(specification.grammar.regex);
  return specification;
}
if (require.main === module) {
  verifySpecification(process.argv[2]);
  console.log("Recorded Conventional Branch specification verified.");
}
module.exports = { verifySpecification };
