// The build of the fixture project: copies the code into dist/, which the test imports, so the test passes only after the build.
import { copyFileSync, mkdirSync } from "node:fs";

mkdirSync(new URL("./dist/", import.meta.url), { recursive: true });
copyFileSync(new URL("./greeting.mjs", import.meta.url), new URL("./dist/greeting.mjs", import.meta.url));
