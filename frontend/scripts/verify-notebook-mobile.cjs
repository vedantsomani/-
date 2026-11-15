const os = require("node:os"),
  path = require("node:path");
const artifactDir = require("node:fs").mkdtempSync(
  path.join(os.tmpdir(), "gamas-notebook-"),
);
process.chdir(artifactDir);
console.log("Artifacts:", artifactDir);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
(async () => {
  const b = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  const p = await b.newPage({ viewport: { width: 390, height: 844 } });
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  await p.goto(
    "http://localhost:5181/?ra=133.76002&dec=-7.24221&name=WISE0855&det=6",
    { waitUntil: "networkidle" },
  );
  await p.getByRole("button", { name: /Image controls/ }).click();
  await p.getByRole("tab", { name: "Visits", exact: true }).click();
  assert.equal(new URL(p.url()).searchParams.get("mode"), "visits");
  assert.equal(
    await p
      .getByRole("button", { name: /Image controls/ })
      .getAttribute("aria-expanded"),
    "false",
  );
  await p.screenshot({ path: "redesign-mobile-final.png", fullPage: true });
  assert(
    await p.evaluate(() => innerWidth >= document.documentElement.scrollWidth),
  );
  await p.setViewportSize({ width: 1440, height: 1000 });
  await p.getByRole("button", { name: "Guided tour" }).click();
  for (let i = 0; i < 5; i++) {
    await p.getByRole("button", { name: "Next", exact: true }).click();
  }
  await p.getByRole("button", { name: "Finish", exact: true }).click();
  console.log(
    "PASS mobile controls, collapsed state, no overflow, complete guided tour",
  );
  await p
    .getByRole("navigation", { name: "Notebook sections" })
    .getByRole("button", { name: /Observe/ })
    .click();
  await p.getByText("Commands", { exact: true }).click();
  const input = p.getByPlaceholder(/d6 difference/);
  await input.fill("d3 visits allwise on");
  await p.getByRole("button", { name: "Run", exact: true }).click();
  assert.equal(new URL(p.url()).searchParams.get("det"), "3");
  assert.equal(new URL(p.url()).searchParams.get("wise"), "1");
  console.log("PASS commands and AllWISE toggle");
  console.log("errors", errors);
  assert.equal(errors.length, 0);
  await b.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
