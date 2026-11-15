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
  const p = await b.newPage({
    viewport: { width: 1440, height: 1000 },
    acceptDownloads: true,
  });
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  await p.goto("http://localhost:5181/", { waitUntil: "networkidle" });
  const image = p.locator(".plate-image>img");
  await image.waitFor();
  assert(await image.evaluate((i) => i.complete && i.naturalWidth > 0));
  const img1 = await image.getAttribute("src");
  await p
    .getByRole("group", { name: "Preview observing season" })
    .getByRole("button")
    .nth(1)
    .click();
  assert.notEqual(await image.getAttribute("src"), img1);
  await p.getByRole("link", { name: "Start observing" }).click();
  await p.getByRole("tab", { name: "Visits", exact: true }).waitFor();
  const c = p.locator('canvas[aria-label^="SPHEREx exposure"]').first();
  await c.waitFor();
  await p
    .getByRole("button", { name: "Pause", exact: true })
    .click()
    .catch(() => {});
  await p.getByRole("tab", { name: "Visits", exact: true }).click();
  await p
    .getByRole("slider", { name: "Observation epoch", exact: true })
    .fill("1");
  await c.click({ position: { x: 150, y: 150 } });
  assert(new URL(p.url()).searchParams.has("sel"));
  await p.getByRole("button", { name: "Side-by-side", exact: true }).click();
  assert.equal(
    await p.locator('canvas[aria-label^="Comparison exposure"]').count(),
    1,
  );
  await p.getByRole("button", { name: "Split wipe", exact: true }).click();
  await p
    .getByRole("slider", { name: "Split wipe curtain position" })
    .fill(".65");
  await p.getByRole("tab", { name: "Difference", exact: true }).click();
  await p
    .getByText("Scale colour by the local noise", { exact: false })
    .click();
  assert(new URL(p.url()).searchParams.has("dlocal"));
  await p
    .getByRole("navigation", { name: "Notebook sections" })
    .getByRole("button", { name: /Evidence/ })
    .click();
  await p
    .getByRole("heading", { name: "What does the evidence say?" })
    .waitFor();
  assert.equal(new URL(p.url()).searchParams.get("section"), "evidence");
  await p.getByRole("button", { name: "Real object", exact: true }).click();
  assert(
    (await p.evaluate(() => Object.keys(localStorage))).some((k) =>
      k.startsWith("spherex_triage_v2:"),
    ),
  );
  await p.reload({ waitUntil: "networkidle" });
  assert(
    await p
      .getByRole("heading", { name: "What does the evidence say?" })
      .isVisible(),
  );
  console.log(
    "PASS landing data, season selection, viewer modes, source selection, evidence, votes, section links",
  );
  await p
    .getByRole("navigation", { name: "Notebook sections" })
    .getByRole("button", { name: /Spectrum/ })
    .click();
  await p
    .getByRole("button", { name: /Play spectral flux sonification/ })
    .waitFor({ timeout: 60000 });
  console.log("PASS spectrum");
  await p
    .getByRole("navigation", { name: "Notebook sections" })
    .getByRole("button", { name: /Prediction/ })
    .click();
  await p.getByRole("button", { name: "Run the prediction test" }).click();
  await p
    .getByRole("slider", { name: "Timeline proper motion scrubber in years" })
    .waitFor({ timeout: 60000 });
  console.log("PASS prediction");
  await p
    .getByRole("navigation", { name: "Notebook sections" })
    .getByRole("button", { name: /Observe/ })
    .click();
  await p.getByRole("button", { name: /Export field/ }).click();
  const d = p.waitForEvent("download");
  await p.getByRole("button", { name: /Download PNG/ }).click();
  await (await d).saveAs("redesign-export.png");
  await p.getByRole("button", { name: "Close discovery card modal" }).click();
  console.log("PASS export");
  await p.getByRole("button", { name: "Guided tour" }).click();
  await p.getByRole("button", { name: "Next", exact: true }).click();
  await p.keyboard.press("Escape");
  console.log("PASS tour");
  await p.setViewportSize({ width: 390, height: 844 });
  await p.screenshot({ path: "redesign-mobile.png", fullPage: true });
  assert(
    await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  );
  console.log("PASS mobile no overflow");
  console.log("errors", errors);
  assert.equal(errors.length, 0);
  await b.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
