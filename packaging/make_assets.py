"""Draws the MSIX icons in packaging/assets from web/assets/logo.svg. Run by hand after the logo changes:

    python packaging/make_assets.py

Chromium draws the logo at each size (MuPDF's SVG reader skips its gradients and clip path).
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "packaging" / "assets"
SVG = (ROOT / "web" / "assets" / "logo.svg").read_text(encoding="utf-8")

# file name -> (width, height, share of the height the logo fills)
ICONS = {f"Square44x44Logo.targetsize-{n}{alt}.png": (n, n, 1.0)
         for n in (16, 24, 32, 48, 256) for alt in ("", "_altform-unplated")}
for scale, f in ((100, 1), (200, 2)):
    ICONS[f"Square44x44Logo.scale-{scale}.png"] = (44 * f, 44 * f, 1.0)
    ICONS[f"Square150x150Logo.scale-{scale}.png"] = (150 * f, 150 * f, 0.66)
    ICONS[f"Wide310x150Logo.scale-{scale}.png"] = (310 * f, 150 * f, 0.66)
    ICONS[f"StoreLogo.scale-{scale}.png"] = (50 * f, 50 * f, 1.0)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        for name, (w, h, share) in ICONS.items():
            side = round(h * share)
            page.set_viewport_size({"width": w, "height": h})
            page.set_content(f"""<style>html,body{{margin:0;background:transparent}}
                body{{width:{w}px;height:{h}px;display:grid;place-items:center}}
                svg{{width:{side}px;height:{side}px;display:block}}</style>{SVG}""")
            page.screenshot(path=str(OUT / name), omit_background=True)
            print("wrote", name)
        browser.close()


if __name__ == "__main__":
    main()
