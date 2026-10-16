"""Rough smoothness numbers for the viewer (not a test; run by hand).

    python tests/e2e/perf.py

Prints: time to first sharp page, frame times while scrolling continuously, how many
page images were requested during a fast flick, and time until a jump target is sharp.
"""
from __future__ import annotations

import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import app_server, browser_page  # noqa: E402
import samples  # noqa: E402

SCROLL_JS = """async ([px, frames]) => {
  const v = document.querySelector('#viewer'); const times = []; let last = performance.now();
  for (let k = 0; k < frames; k++) {
    await new Promise(r => requestAnimationFrame(r));
    const now = performance.now(); times.push(now - last); last = now; v.scrollTop += px;
  }
  return times;
}"""


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    big = samples.build_big(tmp / "big.pdf", pages=400)
    with app_server() as (url, _), browser_page(url) as page:
        requests: list[str] = []
        page.on("request", lambda r: "/page/" in r.url and requests.append(r.url))
        t0 = page.evaluate("performance.now()")
        page.evaluate("(p) => Aurora.openPath(p, {skipGuard: true})", big)
        page.wait_for_function("() => Aurora.V.pages[0]?.hi?.classList.contains('ready')", timeout=30000)
        print(f"open 400 pages -> first sharp page: {page.evaluate('performance.now()') - t0:.0f} ms")

        times = page.evaluate(SCROLL_JS, [30, 240])[5:]
        slow = sum(1 for t in times if t > 20)
        print(f"steady scroll: median frame {statistics.median(times):.1f} ms, p95 {sorted(times)[int(len(times) * .95)]:.1f} ms, "
              f"frames over 20 ms: {slow}/{len(times)}")

        requests.clear()
        page.evaluate(SCROLL_JS, [2500, 60])  # a fast flick through ~60 pages
        hi = [u for u in requests if not u.endswith("/200.jpg")]
        print(f"fast flick: {len(requests)} image requests ({len(hi)} sharp ones)")
        page.wait_for_timeout(800)

        t0 = page.evaluate("performance.now()")
        page.evaluate("Aurora.gotoPage(350, false)")
        page.wait_for_function("() => Aurora.V.pages[350].hi?.classList.contains('ready')", timeout=30000)
        print(f"jump to page 351 -> sharp after {page.evaluate('performance.now()') - t0:.0f} ms")
        print("live pages:", page.evaluate("Aurora.V.pages.filter(p => p.mat).length"), "errors:", page.errors)


if __name__ == "__main__":
    main()
