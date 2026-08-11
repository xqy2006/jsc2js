const fs = require("fs");
const vm = require("vm");
const v8 = require("v8");

const output = process.argv[2];
const expectedV8 = process.argv[3];
if (!output) {
  throw new Error(
    "usage: electron generate_electron_cache.cjs OUTPUT.jsc [EXPECTED_V8]",
  );
}
const actualV8 = process.versions.v8.replace(/-.*$/, "");
if (expectedV8 && actualV8 !== expectedV8) {
  throw new Error(
    `Electron V8 mismatch: expected ${expectedV8}, got ${process.versions.v8}`,
  );
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
  `wrote ${cachedData.length} bytes with verified V8 ${process.versions.v8} to ${output}`,
);
