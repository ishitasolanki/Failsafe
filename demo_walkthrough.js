async page => {
  const BASE = "http://127.0.0.1:8078";
  const beat = ms => page.waitForTimeout(ms);

  // Scroll smoothly so the recording reads as a person reading, not a jump cut.
  const glide = async (toY, ms = 1600) => {
    await page.evaluate(async ([toY, ms]) => {
      const fromY = window.scrollY, dist = toY - fromY, t0 = performance.now();
      const ease = t => t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      await new Promise(done => {
        function step(now) {
          const t = Math.min((now - t0) / ms, 1);
          window.scrollTo(0, fromY + dist * ease(t));
          t < 1 ? requestAnimationFrame(step) : done();
        }
        requestAnimationFrame(step);
      });
    }, [toY, ms]);
  };

  await page.setViewportSize({ width: 1440, height: 900 });

  // 1. the thesis
  await page.goto(BASE, { waitUntil: "networkidle" });
  await beat(4200);

  // 2. the four numbers
  await glide(640);
  await beat(6500);

  // 3. why each failure needs a different answer
  await glide(1180);
  await beat(7000);
  await glide(1560);
  await beat(4500);

  // 4. the cost, reported next to the win
  await glide(2250, 1800);
  await beat(6000);

  // 5. recovery per cause vs the baseline
  await page.click("#tab-recovery");
  await beat(5500);
  await glide(420);
  await beat(6000);

  // 6. does the model earn its place
  await page.click("#tab-diagnosis");
  await beat(1200);
  await glide(300);
  await beat(7500);

  // 7. one payment, end to end
  await page.click("#tab-audit");
  await beat(3000);
  const rows = page.locator(".row");
  const target = rows.filter({ hasText: "FS0038" }).first();
  if (await target.count()) { await target.scrollIntoViewIfNeeded(); await target.click(); }
  await beat(7000);
  await page.locator(".detail").evaluate(el => el.scrollTo({ top: 260, behavior: "smooth" }));
  await beat(6500);

  const revoked = rows.filter({ hasText: "MANDATE_REVOKED" }).first();
  if (await revoked.count()) { await revoked.scrollIntoViewIfNeeded(); await revoked.click(); }
  await beat(6000);

  // 8. the design argument
  await page.click("#tab-how");
  await beat(4000);
  await glide(380);
  await beat(7000);
  await glide(780);
  await beat(6000);
  await glide(0, 1400);
  await beat(2500);
}
