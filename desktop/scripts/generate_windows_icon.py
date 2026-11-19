from pathlib import Path
from PIL import Image
import sys

VARIANTS = {"lime", "lime-invert", "purple", "orange", "pink", "blue", "teal", "rainbow"}
variant = sys.argv[1] if len(sys.argv) > 1 else "lime"
if variant not in VARIANTS:
    raise SystemExit("Unknown icon variant: " + variant + ". Choose one of: " + ", ".join(sorted(VARIANTS)))
ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "Brand book" / "icons" / variant / "square-1024.png"
OUTPUT = ROOT / "desktop" / "src-tauri" / "icons" / "icon.ico"

if not SOURCE.is_file():
    raise SystemExit(f"Brand icon not found: {SOURCE}")

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with Image.open(SOURCE) as image:
    image = image.convert("RGBA")
    image.save(OUTPUT, format="ICO", sizes=[
        (256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)
    ])
print(f"Generated Windows app icon ({variant}): {OUTPUT}")
