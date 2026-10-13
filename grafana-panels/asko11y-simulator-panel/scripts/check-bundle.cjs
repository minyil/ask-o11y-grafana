const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(path.resolve(__dirname, "../dist/module.js"), "utf8");
if (source.includes("new Function") || source.includes("eval(")) {
  throw new Error("built panel contains dynamic code execution");
}
