// The one test of the fixture project, on the code that the build placed in dist/.
import assert from "node:assert/strict";
import { test } from "node:test";
import { greeting } from "./dist/greeting.mjs";

test("greets by name", () => {
  assert.equal(greeting("Ada"), "Hello, Ada");
});
