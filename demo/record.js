async page => {
  // Drives slides and dashboard, holding each scene for exactly as long as its
  // narration WAV runs. Audio is generated first and this paces to it, so the
  // two line up without any editing afterwards.
  const fs = require("fs");
  const path = require("path");
  const BASE = "http://127.0.0.1:8078";
  const SLIDES = "file:///" + path.resolve("demo/slides.html").replace(/\\/g, "/");
  const PAD = 0.35;                    // matches the silence spliced between clips

  const manifest = JSON.parse(fs.readFileSync("demo/audio/manifest.json", "utf8"));
  const hold = s => page.waitForTimeout(Math.round((s + PAD) * 1000));

  const glide = (toY, ms = 1100) => page.evaluate(async ([toY, ms]) => {
    const fromY = window.scrollY, dist = toY - fromY, t0 = performance.now();
    const ease = t => t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    await new Promise(done => {
      (function step(now) {
        const t = Math.min((now - t0) / ms, 1);
        window.scrollTo(0, fromY + dist * ease(t));
        t < 1 ? requestAnimationFrame(step) : done();
      })(performance.now());
    });
  }, [toY, ms]);

  await page.setViewportSize({ width: 1440, height: 900 });

  let at = null;                       // which document is currently loaded
  const slide = async name => {
    await page.goto(SLIDES + "#" + name, { waitUntil: "load" });
    at = "slides";
  };
  const dash = async () => {
    if (at !== "dash") { await page.goto(BASE, { waitUntil: "networkidle" }); at = "dash"; }
  };

  for (const seg of manifest) {
    switch (seg.scene) {
      case "slide-title":   await slide("title");   break;
      case "slide-problem": await slide("problem"); break;
      case "slide-stakes":  await slide("stakes");  break;
      case "broke":         await slide("broke");   break;
      case "broke2":        await slide("broke2");  break;
      case "limits":        await slide("limits");  break;
      case "close":         await slide("close");   break;

      case "hero":
        await dash(); await page.click("#tab-overview"); await glide(0); break;
      case "tiles":
        await glide(660); break;
      case "causes":
        await glide(1240); break;
      case "cost":
        await glide(2260, 1400); break;

      case "recovery":
        await page.click("#tab-recovery"); await glide(0, 500); break;
      case "recovery-scrolled":
        await glide(430); break;

      case "diagnosis":
        if (at === "dash" && !(await page.locator("#panel-diagnosis").isVisible())) {
          await page.click("#tab-diagnosis"); await glide(280);
        }
        break;

      case "audit": {
        await page.click("#tab-audit");
        await page.waitForTimeout(600);
        const row = page.locator(".row").filter({ hasText: "FS0038" }).first();
        if (await row.count()) { await row.scrollIntoViewIfNeeded(); await row.click(); }
        break;
      }
      case "audit-chain":
        await page.locator(".detail").evaluate(el =>
          el.scrollTo({ top: 300, behavior: "smooth" }));
        break;
      case "audit-revoked": {
        const rev = page.locator(".row").filter({ hasText: "MANDATE_REVOKED" }).first();
        if (await rev.count()) { await rev.scrollIntoViewIfNeeded(); await rev.click(); }
        break;
      }

      case "how":
        await page.click("#tab-how"); await glide(0, 500); break;
      case "how-gates":
        await glide(400); break;
    }
    await hold(seg.seconds);
  }
}
