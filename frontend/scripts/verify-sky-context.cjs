const os = require("node:os"),
  path = require("node:path");
const artifactDir = require("node:fs").mkdtempSync(
  path.join(os.tmpdir(), "gamas-notebook-"),
);
process.chdir(artifactDir);
console.log("Artifacts:", artifactDir);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
(async () => {
  const b = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  const p = await b.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  await p.goto(
    "http://localhost:5181/?ra=133.76002&dec=-7.24221&name=WISE0855&section=sky",
    { waitUntil: "networkidle" },
  );
  await p
    .getByRole("button", { name: "Load sky context", exact: false })
    .click();
  await p
    .getByRole("button", { name: "Blink the position at Aladin's centre" })
    .waitFor({ timeout: 30000 });
  console.log("PASS external sky atlas loaded");
  console.log("errors", errors);
  await b.close();
  if (errors.length) process.exitCode = 1;
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
