#!/usr/bin/env python3
"""Generate the matching Slopchik app-icon family and Xcode asset catalogs."""
from pathlib import Path
from PIL import Image, ImageDraw
import json

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "Assets.xcassets"
SIZE = 1024
SCALE = 3
S = SIZE * SCALE

PALETTE = {
    "cream": (247, 239, 222),
    "orange": (224, 91, 42),
    "dark": (38, 37, 35),
    "skin": (224, 169, 126),
    "light": (255, 250, 239),
    "muted": (193, 178, 154),
}

def box(v):
    return tuple(int(x * SCALE) for x in v)

def rounded(draw, xy, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box(xy), radius=int(radius * SCALE), fill=fill, outline=outline, width=max(1, int(width * SCALE)))

def ellipse(draw, xy, fill, outline=None, width=1):
    draw.ellipse(box(xy), fill=fill, outline=outline, width=max(1, int(width * SCALE)))

def polygon(draw, points, fill, outline=None, width=1):
    pts = [(int(x*SCALE), int(y*SCALE)) for x, y in points]
    draw.polygon(pts, fill=fill)
    if outline:
        draw.line(pts + [pts[0]], fill=outline, width=max(1, int(width*SCALE)), joint="curve")

def line(draw, points, fill, width=1):
    draw.line([(int(x*SCALE), int(y*SCALE)) for x,y in points], fill=fill, width=max(1,int(width*SCALE)), joint="curve")

def make_icon(kind):
    if kind == "night":
        bg, accent, secondary = PALETTE["dark"], PALETTE["orange"], (72, 69, 65)
    elif kind == "yard":
        bg, accent, secondary = (235, 224, 201), PALETTE["orange"], (179, 188, 169)
    elif kind == "car":
        bg, accent, secondary = (239, 222, 194), PALETTE["orange"], (205, 196, 177)
    else:
        bg, accent, secondary = PALETTE["cream"], PALETTE["orange"], (233, 214, 184)

    im = Image.new("RGB", (S, S), bg)
    d = ImageDraw.Draw(im)

    # Shared graphic language: soft backdrop disc, thick dark outlines, same character face/cap/glasses.
    ellipse(d, (112, 100, 912, 900), secondary)
    if kind == "night":
        ellipse(d, (145, 130, 880, 865), (53, 51, 48))
        for x,y,r in [(220,220,8),(780,270,6),(735,175,4),(280,720,5)]:
            ellipse(d,(x-r,y-r,x+r,y+r),PALETTE["light"])
    elif kind == "yard":
        # Simplified apartment blocks in the distance.
        for x,y,w,h in [(160,300,190,310),(665,260,190,350)]:
            rounded(d,(x,y,x+w,y+h),18,(207,195,173))
            for wx in range(x+30,x+w-20,55):
                for wy in range(y+45,y+h-25,75):
                    rounded(d,(wx,wy,wx+25,wy+34),5,(133,139,129))
    elif kind == "car":
        # A simple, recognizable compact sedan silhouette behind the mascot.
        rounded(d,(145,635,880,810),52,PALETTE["dark"])
        polygon(d,[(270,640),(380,500),(650,500),(760,640)],PALETTE["dark"])
        polygon(d,[(390,530),(445,530),(445,625),(320,625)],(190,207,205))
        polygon(d,[(470,530),(635,530),(715,625),(470,625)],(190,207,205))
        ellipse(d,(250,745,390,885),PALETTE["dark"])
        ellipse(d,(635,745,775,885),PALETTE["dark"])
        ellipse(d,(292,787,348,843),PALETTE["muted"])
        ellipse(d,(677,787,733,843),PALETTE["muted"])
        rounded(d,(780,680,855,730),12,accent)

    # Torso / track jacket.
    polygon(d,[(280,820),(320,655),(410,590),(615,590),(705,655),(750,820)],PALETTE["dark"])
    polygon(d,[(455,620),(510,620),(560,820),(410,820)],(63,61,57))
    line(d,[(510,640),(510,805)],PALETTE["muted"],5)
    # Neck and face.
    rounded(d,(455,535,570,660),30,PALETTE["skin"])
    ellipse(d,(320,260,700,635),PALETTE["skin"],PALETTE["dark"],12)
    # Ears.
    ellipse(d,(292,405,365,505),PALETTE["skin"],PALETTE["dark"],10)
    ellipse(d,(655,405,728,505),PALETTE["skin"],PALETTE["dark"],10)
    # Hair peeking below cap.
    rounded(d,(350,330,670,440),32,PALETTE["dark"])
    # Cap crown and visor (same on every variant).
    polygon(d,[(315,355),(345,230),(430,170),(575,170),(670,250),(690,355)],PALETTE["dark"])
    polygon(d,[(335,320),(470,290),(695,320),(760,365),(650,395),(430,380)],accent)
    # Tiny cap seam.
    line(d,[(500,190),(500,295)],(91,87,80),6)
    # Sunglasses.
    rounded(d,(365,385,490,455),22,PALETTE["dark"],PALETTE["light"],5)
    rounded(d,(525,385,650,455),22,PALETTE["dark"],PALETTE["light"],5)
    line(d,[(490,415),(525,415)],PALETTE["dark"],12)
    line(d,[(340,405),(365,415)],PALETTE["dark"],9)
    line(d,[(650,415),(680,405)],PALETTE["dark"],9)
    # Nose and confident half-smile.
    ellipse(d,(493,465,525,492),(176,111,82))
    line(d,[(455,535),(495,550),(545,545),(575,525)],PALETTE["dark"],10)
    # Accent badge on jacket.
    rounded(d,(600,700,660,760),14,accent)
    ellipse(d,(620,720,640,740),PALETTE["light"])

    # Small accent label on classic/night; no text to keep icons crisp at small sizes.
    if kind == "classic":
        ellipse(d,(760,185,820,245),accent)
        ellipse(d,(778,203,802,227),PALETTE["light"])

    im = im.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    return im

def write_asset(name, image, app_icon=False):
    folder = ASSETS / (name + (".appiconset" if app_icon else ".imageset"))
    folder.mkdir(parents=True, exist_ok=True)
    image.save(folder / (name + ".png"), format="PNG", optimize=True)
    if app_icon:
        contents = {
            "images": [{"filename": name + ".png", "idiom": "universal", "platform": "ios", "size": "1024x1024"}],
            "info": {"author": "xcode", "version": 1}
        }
    else:
        contents = {
            "images": [{"filename": name + ".png", "idiom": "universal"}],
            "info": {"author": "xcode", "version": 1}
        }
    (folder / "Contents.json").write_text(json.dumps(contents, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

ASSETS.mkdir(parents=True, exist_ok=True)
BRAND_ICONS = ROOT.parent / "Brand book" / "icons"

def write_brand_icon(name, color_folder, app_icon=False):
    source = BRAND_ICONS / color_folder / "square-1024.png"
    image = Image.open(source).convert("RGB")
    write_asset(name, image, app_icon=app_icon)

# Official lime icon is the primary app icon.
write_brand_icon("AppIcon", "lime", app_icon=True)
write_brand_icon("SlopchikClassic", "lime", app_icon=False)

# Official splash and both wordmark colorways.
write_asset("SlopchikSplash", Image.open(ROOT.parent / "Brand book" / "brand" / "splash.png").convert("RGB"))
write_asset("SlopchikLogoDark", Image.open(ROOT.parent / "Brand book" / "brand" / "logo-dark.png").convert("RGB"))
write_asset("SlopchikLogoLight", Image.open(ROOT.parent / "Brand book" / "brand" / "logo-light.png").convert("RGB"))

# All colorways from the brand book are available as alternate icons.
for app_name, folder in [
    ("SlopchikLimeInvert", "lime-invert"),
    ("SlopchikPurple", "purple"),
    ("SlopchikOrange", "orange"),
    ("SlopchikPink", "pink"),
    ("SlopchikBlue", "blue"),
    ("SlopchikTeal", "teal"),
    ("SlopchikRainbow", "rainbow"),
]:
    write_brand_icon(app_name, folder, app_icon=False)
    write_brand_icon(app_name, folder, app_icon=True)
