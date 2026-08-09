const fs = require("fs");
const vm = require("vm");
const v8 = require("v8");

const output = process.argv[2];
if (!output) {
  throw new Error("usage: electron generate_electron_cache.cjs OUTPUT.jsc");
}

// Match the d8 smoke invocation and force nested functions into the cache.
v8.setFlagsFromString("--no-lazy");
const source = `
(function issue23Fixture(seed) {
  function fibonacci(value) {
    if (value < 2) return value + seed;
    return fibonacci(value - 1) + fibonacci(value - 2);
  }
  function makeClosure(offset) {
    return function nested(value) {
      return fibonacci(value) + offset;
    };
  }
  return makeClosure(7)(8);
})(3);
`;
const script = new vm.Script(source, {
  filename: "issue-23-cache-fixture.js",
  produceCachedData: true,
});
const cachedData = script.createCachedData();
if (!Buffer.isBuffer(cachedData) || cachedData.length === 0) {
  throw new Error("Electron did not produce cached data");
}
fs.writeFileSync(output, cachedData);
console.log(
  `wrote ${cachedData.length} bytes with V8 ${process.versions.v8} to ${output}`,
);
