#!/usr/bin/env python3
"""board -- one ink drawing per visual beat, assembled into a motion comic.

Generates one ink line-art panel per frame of a storyboard (<project>/storyboards/<board>.yaml)
through an image-edit model on Atlas Cloud, using character plates from refs.yaml as identity
references, then assembles the picked frames over the narrated cut from `s2c voices` with
page-turn transitions, a slow push-in toward each panel's accent-colored focus, burned-in
subtitles, and music / sound effects from music.yaml and sfx.yaml.

  s2c board gen      <board> [--frames F01a,F01b] [-n 2] [--model nb21d]
  s2c board sheet    <board> [--all]                 contact sheet to pick from
  s2c board edit     <board> F01a "remove the candles"   in-place edit of the pick (a new candidate)
  s2c board fix      <board>                         redraw clock hands (frames with post.clock)
  s2c board assemble <board> [--subs all] [--no-music]   -> out/<output>.mp4
  s2c board status   <board>
  s2c board merge    <name> <video> <video> ...      join finished videos losslessly

Generated images live in <project>/cache/board/<board>/; the board YAML (with its picks) and
ledger-images.jsonl are the versioned record.
"""
from __future__ import annotations

import argparse
import hashlib
NL_ = chr(10)
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

if __package__:
    from . import project
else:                                   # run as a file: python script2comic/board.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from script2comic import project

PROJECT = project.PROJECT
BOARDS_DIR = PROJECT / "storyboards"
REF_DIR = PROJECT / "refs"
REFS_YAML = PROJECT / "refs.yaml"
STYLE_YAML = PROJECT / "style.yaml"
BOARD_CACHE = project.CACHE / "board"
REFS_JSON = BOARD_CACHE / "refs.json"
LEDGER = PROJECT / "ledger-images.jsonl"
OUT_DIR = project.OUT
AUDIOBOOK_OUT = project.OUT
MUSIC_CATALOG, MUSIC_DIR = PROJECT / "music.yaml", PROJECT / "music"
SFX_CATALOG, SFX_DIR = PROJECT / "sfx.yaml", PROJECT / "sfx"

API = project.env("ATLAS_BASE_URL", "https://api.atlascloud.ai/api/v1")

# ---------------------------------------------------------------------------
# Characters: refs.yaml -- one entry per name a frame can list under `refs`
#   mira:
#     files: [mira.png]            # reference plates in <project>/refs/ (or absolute paths); ONE front
#                                  # plate per person, never a turnaround sheet (it duplicates figures)
#     desc: "MIRA: a woman in her 30s, ..."   # the locked descriptor; wardrobe included for period scenes
#   pier:
#     files: [pier.png]
#     desc: "PIER NINE: ..."
#     place: true                  # a place, not a person: redrawn as architecture
# ---------------------------------------------------------------------------
_CHARACTERS: dict | None = None


def characters() -> dict:
    global _CHARACTERS
    if _CHARACTERS is None:
        if not REFS_YAML.exists():
            sys.exit(f"missing {REFS_YAML} (see example/refs.yaml)")
        doc = yaml.safe_load(REFS_YAML.read_text(encoding="utf-8")) or {}
        _CHARACTERS = doc.get("characters") or doc
        for name, spec in _CHARACTERS.items():
            spec.setdefault("files", [])
            if not spec.get("desc"):
                sys.exit(f"refs.yaml: {name} needs a desc")
    return _CHARACTERS


def plate_paths(name: str) -> list[Path]:
    return [Path(f) if Path(f).is_absolute() else REF_DIR / f for f in characters()[name]["files"]]


# ---------------------------------------------------------------------------
# Style: fixed prompt blocks. Any of them can be overridden in <project>/style.yaml
# (keys style, once_rule, anchor_note, no_text); `tints` there names extra blocks a frame can
# switch on with `tint: <name>` (a colour wash for a dream, a memory, a different world).
# ---------------------------------------------------------------------------
STYLE = (
    "Hand-drawn black ink line art in the style of a graphic-novel storyboard panel: "
    "clean confident pen linework, sparse cross-hatching for shadow, a light gray ink wash "
    "for depth, white paper texture. The image is MONOCHROME ink except for ONE accent "
    "color, used only on the focal element described below. Widescreen 16:9 composition."
)
ONCE_RULE = (
    "Each named character appears exactly ONCE in the panel; never duplicate a figure. "
    "A single reference photo per character shows one person, not several."
)
ANCHOR_NOTE = (
    "The LAST reference image is the STYLE ANCHOR: match its ink line weight, hatching, "
    "paper and overall look exactly so every panel reads as the same artist's hand."
)
NO_TEXT = "No text, letters, numbers, captions, speech bubbles, logos or watermarks anywhere."
TINTS: dict = {}
if STYLE_YAML.exists():
    _st = yaml.safe_load(STYLE_YAML.read_text(encoding="utf-8")) or {}
    STYLE = " ".join(str(_st.get("style", STYLE)).split())
    ONCE_RULE = " ".join(str(_st.get("once_rule", ONCE_RULE)).split())
    ANCHOR_NOTE = " ".join(str(_st.get("anchor_note", ANCHOR_NOTE)).split())
    NO_TEXT = " ".join(str(_st.get("no_text", NO_TEXT)).split())
    TINTS = {k: " ".join(str(v).split()) for k, v in (_st.get("tints") or {}).items()}

MODELS = {
    "nb2": {"id": "google/nano-banana-2/edit", "price": 0.08,
            "extra": {"aspect_ratio": "16:9", "resolution": "1k", "output_format": "png"}},
    # Nano Banana 2.1 via Atlas "developer" routing: same inputs, flat price,
    # names the reference field differently and supports naming an element to edit in place.
    "nb21d": {"id": "google/nano-banana-2.1-developer/edit", "price": 0.018, "images_field": "reference_images",
              "extra": {"aspect_ratio": "16:9", "resolution": "1k"}},
    "nb2d": {"id": "google/nano-banana-2/edit-developer", "price": 0.028,
             "extra": {"aspect_ratio": "16:9", "resolution": "1k"}},
    "nb2-2k": {"id": "google/nano-banana-2/edit", "price": 0.08,
               "extra": {"aspect_ratio": "16:9", "resolution": "2k", "output_format": "png"}},
    "sd45": {"id": "bytedance/seedream-v4.5/edit", "price": 0.036,
             "extra": {"size": "2848*1600"}},
    "sd5lite": {"id": "bytedance/seedream-v5.0-lite/edit", "price": 0.04,
                "extra": {"size": "2848*1600"}},
}
# Measured on a 14-core laptop CPU: 4 encodes = ~50% CPU, ~1 GB each, so ~2.5 threads per job --
# zoompan is single-threaded and x264 only partly fills the gap.
import os
DEFAULT_PARALLEL = max(2, min(8, (os.cpu_count() or 8) // 2))

def nvenc_available(name: str = "hevc_nvenc") -> bool:
    """Idle-machine benchmark (laptop i9 + RTX 4060): a 20 s zoompan clip encodes at
    252 fps on h264_nvenc vs 48 fps on libx264 fast, so the GPU is the default when present."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return False
    return name in out


# Burned-in subtitles (ASS via libass). Speaker name in bold accent gold, line in white.
# Colors are ASS &HBBGGRR&; gold #C9A84C -> 4CA8C9 (matches the clock-hand accent).
SUB_FONT, SUB_SIZE, SUB_MARGIN_V, SUB_OUTLINE = "Arial", 66, 110, 4.5
SUB_NAME_COLOR = "4CA8C9"
BS = chr(92)

ENCODERS = {   # concat demuxer needs one encoder per run, so the choice tags clip/segment names
    "x264": ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"],
    "nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"],
    # HEVC: ~half the size of H.264 at matching quality; hvc1 tag keeps Apple players happy.
    "hevc": ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "22", "-b:v", "0", "-pix_fmt", "yuv420p",
             "-tag:v", "hvc1"],
}
DONE = {"completed", "succeeded"}
FAILED = {"failed", "cancelled", "expired"}


def headers():
    return {"Authorization": f"Bearer {project.env('ATLAS_API_KEY', required=True)}"}


# ---------------------------------------------------------------------------
# Board + refs
# ---------------------------------------------------------------------------
def load_board(board_id: str) -> dict:
    p = BOARDS_DIR / f"{board_id}.yaml"
    if not p.exists():
        sys.exit(f"no board {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def save_board(board: dict) -> None:
    p = BOARDS_DIR / f"{board['id']}.yaml"
    p.write_text(yaml.safe_dump(board, sort_keys=False, allow_unicode=True, width=88),
                 encoding="utf-8")


def frame_dir(board_id: str) -> Path:
    d = BOARD_CACHE / board_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def upload(path: Path) -> str:
    """Atlas temporary media URL for a local file (cached in refs.json)."""
    BOARD_CACHE.mkdir(parents=True, exist_ok=True)
    cache = json.loads(REFS_JSON.read_text(encoding="utf-8")) if REFS_JSON.exists() else {}
    key = str(path.resolve())
    hit = cache.get(key)
    if hit and time.time() - hit["t"] < 6 * 3600:
        return hit["url"]
    with path.open("rb") as f:
        r = requests.post(f"{API}/model/uploadMedia", headers=headers(),
                          files={"file": (path.name, f)}, timeout=120)
    if r.status_code != 200:
        sys.exit(f"uploadMedia {r.status_code}: {r.text[:300]}")
    url = r.json()["data"]["download_url"]
    cache[key] = {"url": url, "t": time.time()}
    REFS_JSON.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    print(f"  uploaded {path.name}")
    return url


_BOARD_PICKS: dict = {}     # frame id -> picked image path, filled by cmd_gen for continuity refs

def ref_images(names: list[str], anchor: Path | None) -> tuple[list[str], str]:
    """Upload the plates for the named characters (+ style anchor); return
    (urls, identity text that tells the model which images are whom)."""
    urls, lines, i = [], [], 1
    for n in names:
        if n.startswith("@"):                     # continuity ref: an approved frame of this board
            fid = n[1:]
            if "/" in fid:                        # another board's frame, e.g. @act-two/F44h for a flashback
                bid, fid = fid.split("/", 1)
                ob = load_board(bid)
                of = next((x for x in ob["frames"] if x["id"] == fid), None)
                src = pick_frame(ob, of, "nb21d") if of else None
            else:
                src = _BOARD_PICKS.get(fid)
            if src is None or not src.exists():
                sys.exit(f"continuity ref {n}: frame {fid} has no picked/generated image yet")
            urls.append(upload(src))
            lines.append(f"image {i} = an earlier panel from this story: keep the SAME place (architecture, "
                         f"furniture, props, lighting) and the SAME costumes on the people who appear in both; "
                         f"a different camera angle is fine.")
            i += 1
            continue
        spec = characters().get(n)
        if not spec:
            sys.exit(f"unknown ref {n!r}; add it to {REFS_YAML}")
        files = plate_paths(n)
        idx = []
        for f in files:
            if not f.exists():
                sys.exit(f"missing plate {f}")
            urls.append(upload(f))
            idx.append(i)
            i += 1
        which = f"image {idx[0]}" if len(idx) == 1 else f"images {idx[0]}-{idx[-1]}"
        if spec.get("place"):
            lines.append(f"{which} = {spec['desc']}. This is a PLACE, not a person: redraw the same architecture and "
                         f"layout in the ink style, as a drawing, never as a photo.")
            continue
        lines.append(f"{which} = {spec['desc']}. Draw this exact person ONCE, in ink: same face "
                     f"shape, hairstyle and features, recognizable from the photo.")
    if anchor is not None:
        urls.append(upload(anchor))
        lines.append(ANCHOR_NOTE)
    return urls, " ".join(lines)


def build_prompt(frame: dict, identity: str) -> str:
    parts = [STYLE]
    if not frame.get("multi"):
        parts.append(ONCE_RULE)
    if frame.get("tint"):
        if frame["tint"] not in TINTS:
            sys.exit(f"{frame.get('id')}: tint {frame['tint']!r} is not defined under `tints` in {STYLE_YAML}")
        parts.append(TINTS[frame["tint"]])
    if identity:
        parts.append("REFERENCES: " + identity)
    parts.append("SCENE: " + " ".join(frame["prompt"].split()))
    if frame.get("shot"):
        parts.append(f"SHOT: {frame['shot']}.")
    focus = frame.get("focus", "")
    if focus and not focus.startswith("none"):
        parts.append(f"ACCENT COLOR only on: {focus}. STRICT: nothing else in the panel has any color — "
                     f"clothing, skin, windows, lamps and light sources all stay gray ink on white paper.")
    if frame.get("text"):
        parts.append(f"The ONLY text in the image, hand-lettered and legible: \"{frame['text']}\". "
                     f"No other words anywhere.")
    else:
        parts.append(NO_TEXT)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def generate(model_key: str, prompt: str, images: list[str], seed: int | None) -> tuple[str, dict]:
    m = MODELS[model_key]
    payload = {"model": m["id"], "prompt": prompt, m.get("images_field", "images"): images, **m["extra"]}
    if seed is not None and model_key.startswith("nb2"):
        payload["seed"] = seed
    for attempt in range(4):
        r = requests.post(f"{API}/model/generateImage", headers={**headers(), "Content-Type": "application/json"},
                          json=payload, timeout=120)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(5 * (attempt + 1))
            continue
        break
    if r.status_code >= 400:
        raise RuntimeError(f"generateImage {r.status_code}: {r.text[:400]}")
    body = r.json()
    data = body.get("data", body)
    task_id = data.get("id") or data.get("prediction_id")
    outputs = data.get("outputs") or []
    t0 = time.time()
    while not outputs:
        if time.time() - t0 > 600:
            raise RuntimeError(f"timeout waiting for {task_id}")
        time.sleep(4)
        g = requests.get(f"{API}/model/prediction/{task_id}", headers=headers(), timeout=60)
        if g.status_code >= 400:
            raise RuntimeError(f"prediction {g.status_code}: {g.text[:300]}")
        data = g.json().get("data", g.json())
        status = str(data.get("status", "")).lower()
        if status in FAILED:
            raise RuntimeError(f"prediction {task_id} {status}: {json.dumps(data)[:400]}")
        if status in DONE:
            outputs = data.get("outputs") or []
            if not outputs:
                raise RuntimeError(f"finished with no outputs: {json.dumps(data)[:400]}")
    return outputs[0], data


def candidates(board_id: str, fid: str) -> list[Path]:
    return sorted(frame_dir(board_id).glob(f"{fid}-*.png"))


def next_index(board_id, fid, mk):
    """Next candidate number for <fid>-<mk>-N.png: max existing N + 1 (never reuses a name,
    even after rejects are moved away, so an edit can never overwrite its own source)."""
    ns = [int(m.group(1)) for c in frame_dir(board_id).glob(f"{fid}-{mk}-*.png")
          if (m := re.match(rf"{re.escape(fid)}-{re.escape(mk)}-(\d+)(?:-|\.)", c.name))]
    rej = frame_dir(board_id) / "rejected"
    if rej.exists():
        ns += [int(m.group(1)) for c in rej.glob(f"{fid}-{mk}-*.png")
               if (m := re.match(rf"{re.escape(fid)}-{re.escape(mk)}-(\d+)(?:-|\.)", c.name))]
    return max(ns, default=0) + 1


def cmd_gen(args):
    board = load_board(args.board)
    want = set(args.frames.split(",")) if args.frames else None
    frames = [f for f in board["frames"] if not want or f["id"] in want]
    for f0 in board["frames"]:
        c0 = candidates(board["id"], f0["id"])
        if f0.get("pick"):
            _BOARD_PICKS[f0["id"]] = frame_dir(board["id"]) / f0["pick"]
        elif c0:
            _BOARD_PICKS[f0["id"]] = c0[0]
    model_keys = args.model.split(",")
    anchor = None
    if args.anchor:
        anchor = Path(args.anchor)
    elif board.get("anchor"):
        anchor = frame_dir(board["id"]) / board["anchor"]
    if anchor is not None and not anchor.exists():
        sys.exit(f"style anchor not found: {anchor}")
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    lock = threading.Lock()
    spent = [0.0]
    # pre-upload every plate serially so threads never race on refs.json
    for f in frames:
        for n in f.get("refs") or []:
            if n.startswith("@"):
                continue
            if n not in characters():
                sys.exit(f"{f['id']}: unknown ref {n!r}; add it to {REFS_YAML}")
            for fn in plate_paths(n):
                upload(fn)
    if anchor is not None:
        upload(anchor)
    # Frames referenced by "@id" from other frames in this run are generated in a first pass,
    # so continuity anchors exist before the frames that lean on them are prompted.
    wanted_ids = [f["id"] for f in frames]
    anchors = [f for f in frames if any(f"@{f['id']}" in (g.get("refs") or []) for g in frames if g is not f)]
    if anchors and any(a["id"] not in _BOARD_PICKS for a in anchors):
        print(f"  first pass: {len(anchors)} continuity anchors ({', '.join(a['id'] for a in anchors)})")
        sub = argparse.Namespace(**vars(args)); sub.frames = ",".join(a["id"] for a in anchors)
        cmd_gen(sub)
        for a in anchors:
            c0 = candidates(board["id"], a["id"])
            if c0: _BOARD_PICKS[a["id"]] = c0[0]
        frames = [f for f in frames if f not in anchors]
    tasks = []
    for f in frames:
        for mk in model_keys:
            existing = [p for p in candidates(board["id"], f["id"]) if f"-{mk}-" in p.name]
            if existing and not args.force:
                print(f"  {f['id']} {mk}: {len(existing)} cached, skip (--force to add)")
                continue
            urls, identity = ref_images(f.get("refs") or [], anchor)
            if not urls:
                # the edit model needs at least one image: the style anchor, else any plate
                fallback = anchor or next((pp for nm in characters() for pp in plate_paths(nm) if pp.exists()), None)
                if fallback is None:
                    sys.exit(f"{f['id']}: no refs, no anchor and no plates in refs.yaml; give the board an `anchor`")
                urls = [upload(fallback)]
                identity = (ANCHOR_NOTE if anchor else
                            "The reference image is only a style/quality reference; do NOT draw that person.")
            prompt = build_prompt(f, identity)
            for k in range(args.n):
                tasks.append((f, mk, k, prompt, urls))

    def run(task):
        f, mk, k, prompt, urls = task
        seed = (abs(hash((f["id"], k))) % 2_000_000_000) if args.seeded else None
        url, raw = generate(mk, prompt, urls, seed)
        content = requests.get(url, timeout=300).content
        with lock:
            n = next_index(board["id"], f["id"], mk)
            dest = frame_dir(board["id"]) / f"{f['id']}-{mk}-{n}.png"
            dest.write_bytes(content)
            cost = MODELS[mk]["price"]
            spent[0] += cost
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with LEDGER.open("a", encoding="utf-8") as lf:
                lf.write(json.dumps({"t": int(time.time()), "board": board["id"], "frame": f["id"],
                                     "model": MODELS[mk]["id"], "cost_usd": cost,
                                     "file": dest.name, "prediction": raw.get("id")}) + "\n")
            print(f"  {f['id']} {mk} #{k+1}: {dest.name}  (${spent[0]:.2f} this run)", flush=True)

    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = {ex.submit(run, t): t for t in tasks}
        for fut in as_completed(futs):
            f, mk, k = futs[fut][:3]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  {f['id']} {mk} #{k+1}: FAILED {e}", flush=True)
    spent = spent[0]
    print(f"done; ~${spent:.2f} this run")


def cmd_refs(args):
    board = load_board(args.board)
    names = sorted({n for f in board["frames"] for n in (f.get("refs") or []) if not n.startswith("@")})
    for n in names:
        for fn in plate_paths(n):
            upload(fn)
    print(f"refs ready for {', '.join(names)}")


# ---------------------------------------------------------------------------
# Post-fixes: things image models get wrong but geometry gets right
# ---------------------------------------------------------------------------
def find_dial(img, override=None):
    """(cx, cy, r) of a clock face: Hough circles that lie WHOLLY inside the image, scored by
    a bright centre, some ink in the numeral ring and a dark rim. A YAML override
    {cx, cy, r} wins when given. No size bias -- a wall clock in a two-shot is small."""
    import cv2, numpy as np
    if override and all(k in override for k in ("cx", "cy", "r")):
        return int(override["cx"]), int(override["cy"]), int(override["r"])
    g = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    h, w = g.shape
    blur = cv2.GaussianBlur(g, (7, 7), 1.5)
    cands = []
    for p2 in (60, 45, 32, 24):
        c = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1.2, minDist=h // 10, param1=120,
                             param2=p2, minRadius=int(0.05 * h), maxRadius=int(0.45 * h))
        if c is not None:
            cands += [tuple(int(v) for v in cc) for cc in c[0]]
    cands = list(dict.fromkeys(cands))                       # every threshold, deduped
    yy, xx = np.ogrid[:h, :w]
    best, best_score = None, -9.0
    for cx, cy, r in cands:
        if cx - r < 2 or cy - r < 2 or cx + r > w - 2 or cy + r > h - 2:
            continue
        d2 = (xx - cx) ** 2 + (yy - cy) ** 2
        inner = g[d2 < (0.5 * r) ** 2]
        ring = g[(d2 > (0.62 * r) ** 2) & (d2 < (0.9 * r) ** 2)]
        rim = g[(d2 > (0.95 * r) ** 2) & (d2 < (1.06 * r) ** 2)]
        if inner.size < 50 or rim.size < 50:
            continue
        score = ((inner.mean() - rim.mean()) / 255.0 + 0.25 * float((ring < 110).mean() > 0.015)
                 + 0.15 * r / h)                               # mild tie-break toward the bigger dial
        if score > best_score:
            best, best_score = (cx, cy, r), score
    if best is None:
        raise RuntimeError("no clock dial found; give post.clock cx/cy/r in the board")
    return best


def rebuild_twelve(orig, inkish, clean, cx, cy, r, face_col):
    """The old hand sat on the 12: clear what is left of it and compose a new "12" from
    the untouched "1" (one o'clock) and "2" (two o'clock) glyphs of the same clock."""
    import cv2, numpy as np
    h, w = inkish.shape
    yy, xx = np.mgrid[:h, :w]
    rad = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    cw = (np.degrees(np.arctan2(yy - cy, xx - cx)) + 90) % 360
    band = (rad > 0.62 * r) & (rad < 0.80 * r)          # numerals live here; ticks/hatching start ~0.82 r
    clear_band = (rad > 0.6 * r) & (rad < 0.9 * r)

    def glyph(hour):
        d = np.abs((cw - hour * 30 + 180) % 360 - 180)
        m = inkish & band & (d < 7)
        # keep only compact blobs (glyph strokes), never thin arcs of rim or hatching
        # the numeral is the LARGEST compact blob at its hour; tick marks and rim hatching
        # are compact too but small, so take just the biggest one
        n, lab = cv2.connectedComponents(m.astype(np.uint8), connectivity=8)
        best, best_n = None, 0
        for k in range(1, n):
            blob = lab == k
            cnt = int(blob.sum())
            bys, bxs = np.nonzero(blob)
            span = max(bys.max() - bys.min(), bxs.max() - bxs.min())
            if span < 0.22 * r and cnt > best_n:
                best, best_n = blob, cnt
        m = best if best is not None else np.zeros_like(m)
        if m.sum() < 20:
            return None
        ys, xs = np.nonzero(m)
        y0, y1, x0, x1 = max(0, ys.min() - 2), ys.max() + 3, max(0, xs.min() - 2), xs.max() + 3
        mm = cv2.dilate(m[y0:y1, x0:x1].astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        return orig[y0:y1, x0:x1], mm, float(rad[ys, xs].mean())
    g1, g2 = glyph(1), glyph(2)
    if g1 is None or g2 is None:
        return clean
    d0 = np.abs((cw + 180) % 360 - 180)
    diff = np.abs(clean.astype(np.int16) - face_col.astype(np.int16)).sum(axis=2)
    old = ((diff > 60) & clear_band & (d0 < 10)).astype(np.uint8) * 255
    old = cv2.dilate(old, np.ones((5, 5), np.uint8), iterations=1)
    clean = cv2.inpaint(clean, old, 5, cv2.INPAINT_TELEA)
    R = (g1[2] + g2[2]) / 2
    (c1, m1, _), (c2, m2, _) = g1, g2
    gap = max(2, int(0.12 * (m1.shape[1] + m2.shape[1]) / 2))
    x1 = int(round(cx - gap / 2 - m1.shape[1]))
    x2 = int(round(cx + gap / 2))
    for crop, m, x in ((c1, m1, x1), (c2, m2, x2)):
        y = int(round(cy - R - m.shape[0] / 2))
        reg = clean[y:y + m.shape[0], x:x + m.shape[1]]
        if reg.shape[:2] == m.shape:
            reg[m] = crop[m]
    return clean


def fix_clock(src, dst, time_str, seconds=None, override=None, accent=True, gold=(201, 168, 76), reerase=False):
    """Erase only the ink connected to the dial centre (the hands, never the numerals),
    inpaint, then draw hour/minute(/second) hands at the true angles, anti-aliased."""
    import cv2, numpy as np, math
    from PIL import Image, ImageDraw
    img = Image.open(src).convert("RGB")
    cx, cy, r = find_dial(img, override)
    a = np.asarray(img).copy()
    h, w = a.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    d2 = (xx - cx) ** 2 + (yy - cy) ** 2
    face = d2 < (0.92 * r) ** 2
    face_col = np.median(a[face].reshape(-1, 3), axis=0)          # white, or green under the Veil
    diff = np.abs(a.astype(np.int16) - face_col.astype(np.int16)).sum(axis=2)
    inkish = (diff > 90) & face
    n, labels = cv2.connectedComponents(inkish.astype(np.uint8), connectivity=8)
    centre = (d2 < (0.16 * r) ** 2) & inkish
    hand_labels = [int(v) for v in np.unique(labels[centre]) if v != 0]
    comp = np.isin(labels, hand_labels)
    # Erase only along the old hands: find their directions from the component pixels in
    # the inner annulus, then keep the mask to narrow wedges around those angles (plus the
    # hub), so a numeral the old hand touched at the rim survives.
    ys, xs = np.nonzero(comp)
    rad = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    ang = np.degrees(np.arctan2(ys - cy, xs - cx)) % 360
    sel = (rad > 0.22 * r) & (rad < 0.6 * r)
    hist = np.bincount((ang[sel] // 6).astype(int), minlength=60)
    peaks = [i * 6 + 3 for i in range(60) if hist[i] > 0.15 * hist.max()] if hist.max() > 0 else []
    near = np.zeros(len(xs), dtype=bool)
    for pk in peaks:
        d_ang = np.abs((ang - pk + 180) % 360 - 180)
        # wide near the hub (hands taper from a broad base), a sliver out by the numerals
        near |= (d_ang < 11) & (rad < 0.6 * r)
        perp = rad * np.sin(np.radians(np.minimum(d_ang, 90)))
        near |= (perp < 0.035 * r) & (d_ang < 30) & (rad >= 0.6 * r)
    keep = near | (rad < 0.2 * r)
    mask = np.zeros((h, w), np.uint8)
    mask[ys[keep], xs[keep]] = 255
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
    maxr = (override or {}).get("maxr")         # keep the eraser off whatever overlaps the dial edge
    if maxr:
        mask[d2 >= (maxr * r) ** 2] = 0
    for x1, y1, x2, y2, wd in (override or {}).get("erase", []):   # stray strokes off the hub (e.g. an off-center model pivot)
        cv2.line(mask, (int(x1), int(y1)), (int(x2), int(y2)), 255, int(wd))
    if reerase:
        hh0, mm0 = (int(x) for x in time_str.split(":"))
        s0 = seconds or 0
        old = [((hh0 % 12 + mm0 / 60 + s0 / 3600) * 30, 0.50 * r, max(3, int(0.06 * r)) + 3),
               ((mm0 + s0 / 60) * 6, 0.74 * r, max(2, int(0.045 * r)) + 3)]
        if seconds is not None:
            old.append((s0 * 6, 0.84 * r, max(1, int(0.015 * r)) + 3))
        for ang0, ln0, wd0 in old:
            t0 = math.radians(ang0 - 90)
            cv2.line(mask, (int(cx), int(cy)), (int(round(cx + ln0 * math.cos(t0))), int(round(cy + ln0 * math.sin(t0)))),
                     255, int(wd0) + 6)
        cv2.circle(mask, (int(cx), int(cy)), max(3, int(0.055 * r)) + 4, 255, -1)
    clean = cv2.inpaint(a, mask, 5, cv2.INPAINT_TELEA)
    hit = set()
    for pk in peaks:
        cw = (pk + 90) % 360                                  # clockwise from 12
        hr = int(round(cw / 30)) % 12
        if abs((cw - hr * 30 + 180) % 360 - 180) < 9:
            hit.add(hr)
    if 0 in hit and not (override or {}).get("keep12"):    # tick dials have no 12 glyph to rebuild
        clean = rebuild_twelve(a, inkish, clean, cx, cy, r, face_col)
    out = Image.fromarray(clean).convert("RGBA")
    S = 4
    layer = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    hh, mm = (int(x) for x in time_str.split(":"))
    sec = seconds or 0
    # Tapered hands with a pointed tip and a short counterweight tail (not flat-ended bars).
    # (angle, length, half-width at the hub, tail length)
    hands = [((hh % 12 + mm / 60 + sec / 3600) * 30, 0.50 * r, max(2.2, 0.045 * r), 0.12 * r),
             ((mm + sec / 60) * 6, 0.68 * r, max(1.6, 0.032 * r), 0.14 * r)]   # stops short of the numerals
    if seconds is not None:
        hands.append((sec * 6, 0.80 * r, max(0.8, 0.010 * r), 0.20 * r))
    ink = (28, 24, 20, 255)
    fill = (gold + (255,)) if accent else (70, 64, 58, 255)
    o = max(1.0, 0.014 * r)                                   # outline thickness

    def hand_poly(ang, length, hw, tail, grow=0.0):
        t = math.radians(ang - 90)
        ux, uy = math.cos(t), math.sin(t)
        px, py = -uy, ux
        L, H, T = length + 3.0 * grow, hw + grow, tail + grow    # tip half-angle ~19 deg -> axial offset ~3x
        pts = [(-T, 0.55 * H), (0.0, H), (0.80 * L, 0.62 * H), (L, 0.0),
               (0.80 * L, -0.62 * H), (0.0, -H), (-T, -0.55 * H)]
        return [((cx + a_ * ux + b_ * px) * S, (cy + a_ * uy + b_ * py) * S) for a_, b_ in pts]

    for ang, length, hw, tail in hands:
        d.polygon(hand_poly(ang, length, hw, tail, grow=o), fill=ink)
        d.polygon(hand_poly(ang, length, hw, tail), fill=fill)
    cap = max(2.5, 0.06 * r) * S
    d.ellipse([cx * S - cap, cy * S - cap, cx * S + cap, cy * S + cap], fill=ink)
    pin = max(1.0, 0.022 * r) * S
    d.ellipse([cx * S - pin, cy * S - pin, cx * S + pin, cy * S + pin], fill=fill)
    layer = layer.resize((w, h), Image.LANCZOS)
    out.alpha_composite(layer)
    out.convert("RGB").save(dst)
    return cx, cy, r


def cmd_fix(args):
    board = load_board(args.board)
    want = set(args.frames.split(",")) if args.frames else None
    changed = 0
    for f in board["frames"]:
        post = f.get("post") or {}
        if "clock" not in post or (want and f["id"] not in want):
            continue
        src = pick_frame(board, f, args.model)
        if src.stem.endswith("-clock"):                       # already fixed; refix from its source
            src = src.with_name(src.name.replace("-clock", ""))
        onto = None
        if post.get("from"):        # e.g. the pick is a model edit of a fixed frame: fix the clean original...
            onto = frame_dir(board["id"]) / post.get("onto", src.name)
            src = frame_dir(board["id"]) / post["from"]
        dst = (onto or src).with_name((onto or src).stem + "-clock.png")
        cx, cy, r = fix_clock(src, dst, post["clock"], post.get("seconds"), post, accent=post.get("accent", True),
                              reerase=bool(post.get("reerase")))
        if onto is not None:        # ...then paste only the dial disk into the edited frame
            from PIL import Image, ImageDraw
            base = Image.open(onto).convert("RGB"); fixed = Image.open(dst).convert("RGB")
            m = Image.new("L", base.size, 0); rr = int(r * 1.04)
            ImageDraw.Draw(m).ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=255)
            from PIL import ImageFilter; m = m.filter(ImageFilter.GaussianBlur(2))   # feather: no seam on the wall
            base.paste(fixed, (0, 0), m); base.save(dst)
        f["pick"] = dst.name
        changed += 1
        print(f"  {f['id']}: clock {post['clock']}{':%02d' % post['seconds'] if post.get('seconds') is not None else ''} "
              f"dial@({cx},{cy}) r={r} -> {dst.name}")
    if changed:
        save_board(board)
    print(f"{changed} frame(s) fixed; picks updated")


# ---------------------------------------------------------------------------
# Review sheet
# ---------------------------------------------------------------------------
def cmd_sheet(args):
    """Contact sheet for review. Default: the current pick (or first candidate) of each
    frame, 3 per row at 640 px, so twelve frames fit one legible look; --all shows every
    candidate; --frames limits to a comma-separated subset; --name tags the output file."""
    from PIL import Image, ImageDraw
    board = load_board(args.board)
    want = set(args.frames.split(",")) if args.frames else None
    tiles, labels = [], []
    for f in board["frames"]:
        if want and f["id"] not in want:
            continue
        cands = candidates(board["id"], f["id"])
        if not cands:
            continue
        if args.all:
            chosen = cands
        elif f.get("pick") and (frame_dir(board["id"]) / f["pick"]).exists():
            chosen = [frame_dir(board["id"]) / f["pick"]]
        else:
            chosen = cands[:1]
        for c in chosen:
            tiles.append(c)
            labels.append(c.stem + ("  *" if f.get("pick") == c.name else ""))
    if not tiles:
        sys.exit("no candidates yet")
    w, cols = args.tile, args.cols
    h = int(w * 9 / 16)
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * w, rows * (h + 26)), "white")
    d = ImageDraw.Draw(sheet)
    for i, (p, lab) in enumerate(zip(tiles, labels)):
        im = Image.open(p).convert("RGB")
        im.thumbnail((w, h))
        x, y = (i % cols) * w, (i // cols) * (h + 26)
        sheet.paste(im, (x + (w - im.width) // 2, y))
        d.text((x + 6, y + h + 6), lab, fill="black")
    out = frame_dir(board["id"]) / f"_sheet{('_' + args.name) if args.name else ''}.png"
    sheet.save(out)
    print(f"contact sheet: {out}  ({len(tiles)} tiles, {cols} per row)")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def focus_center(path: Path) -> tuple[float, float]:
    """Center of the accent-colored region (fraction of width/height). The
    panel is ink except the focus, so the saturated pixels ARE the focus."""
    from PIL import Image
    im = Image.open(path).convert("HSV").resize((320, 180))
    px = im.load()
    xs, ys = [], []
    for y in range(180):
        for x in range(320):
            h, s, v = px[x, y]
            if s > 90 and v > 70:
                xs.append(x)
                ys.append(y)
    if len(xs) < 40:
        return 0.5, 0.5
    xs.sort(); ys.sort()
    # trim outliers so a stray colored pixel doesn't drag the center
    lo, hi = len(xs) // 20, len(xs) - len(xs) // 20
    return (sum(xs[lo:hi]) / max(1, hi - lo)) / 320, (sum(ys[lo:hi]) / max(1, hi - lo)) / 180


def pick_frame(board: dict, f: dict, model_key: str) -> Path:
    if f.get("pick"):
        p = frame_dir(board["id"]) / f["pick"]
        if p.exists():
            return p
    cands = [p for p in candidates(board["id"], f["id"]) if f"-{model_key}-" in p.name] \
        or candidates(board["id"], f["id"])
    if not cands:
        sys.exit(f"no candidate for {f['id']}; run gen first")
    return cands[0]


def clean_line(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", "", text)
    # a standalone "..." at the start or after a full stop is the spoken-pause marker ((beat) /
    # [pause]); it is timing, not words, so the subtitle drops it. A mid-sentence "..." stays.
    text = re.sub(r"(^|[.!?])\s*\.\.\.(?=\s|$)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


NARR_MAX_CHARS = 95      # two lines at the narration size


def narration_pieces(text: str) -> list[str]:
    """Sentences, merged while they fit two lines; a very long sentence breaks at the comma nearest its middle."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        while len(sent) > NARR_MAX_CHARS + 20:
            cands = [m.end() for m in re.finditer(r"[,;:]\s", sent) if 25 < m.end() < len(sent) - 25]
            if not cands:
                break
            cut = min(cands, key=lambda q: abs(q - len(sent) / 2))
            out.append(sent[:cut].strip()); sent = sent[cut:].strip()
        if out and len(out[-1]) + 1 + len(sent) <= NARR_MAX_CHARS:
            out[-1] += " " + sent
        else:
            out.append(sent)
    return [x for x in out if x]


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs % 360000 // 6000:02d}:{cs % 6000 // 100:02d}.{cs % 100:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def speaker_name(cue: str) -> str:
    """'TOSHI'S MOTHER' -> "Toshi's Mother" (str.title() gives "Toshi'S"): capitalise each word's first letter only."""
    return " ".join(w[:1].upper() + w[1:].lower() for w in cue.split(" "))


CHIME_LEAD = 0.40 + 0.61 + 0.40


def cut_names(cut, listener):
    """(cut to play, full cut the anchors are written against). The listener cut is the same script
    with [[skip]] paragraphs dropped and headings shortened: `s2c voices --only all`."""
    if not listener:
        return cut, cut
    base = cut[:-len("-narrated")] if cut.endswith("-narrated") else cut
    return f"{base}-listener", cut


def default_cut() -> str:
    tls = sorted(AUDIOBOOK_OUT.glob("*-narrated.timeline.json"))
    if len(tls) == 1:
        return tls[0].name[:-len(".timeline.json")]
    if not tls:
        sys.exit(f"no narrated cut in {AUDIOBOOK_OUT}: run `s2c voices` first")
    sys.exit(f"several narrated cuts in {AUDIOBOOK_OUT}; pass --cut <name>")


def load_timelines(cut, listener):
    cut = cut or default_cut()
    play, full = cut_names(cut, listener)
    paths = [AUDIOBOOK_OUT / f"{n}.timeline.json" for n in (play, full)]
    for pth in paths:
        if not pth.exists():
            sys.exit(f"need {pth} (run `s2c voices{' --only all' if listener else ''}` first)")
    tl = json.loads(paths[0].read_text(encoding="utf-8"))
    full_tl = tl if not listener else json.loads(paths[1].read_text(encoding="utf-8"))
    if listener:
        a = sorted(c["line"] for c in tl if c["kind"] == "scene")
        b = sorted(c["line"] for c in full_tl if c["kind"] == "scene")
        if a != b:
            sys.exit("the listener and full timelines come from different script revisions: "
                     "re-render both with `s2c voices --only all`")
    return AUDIOBOOK_OUT / f"{play}.mp3", tl, full_tl


class Anchors:
    """Text-prefix anchors (frames, sound cues) are written against the FULL narrated cut. They are
    resolved there, then mapped by source line onto the cut being played. A line the listener cut
    dropped maps to a neighbouring line of the same scene: `back` (the line before; frames, so each
    surviving line keeps its own picture at its start) or `forward` (the line after; sound effects,
    which then land where the dropped line would have been)."""

    def __init__(self, tl, full_tl, lo=None, hi=None):
        ok = (lambda c: lo <= c["scene"] <= hi) if lo is not None else (lambda c: True)
        self.cut = [c for c in tl if ok(c)]
        self.full = [c for c in full_tl if ok(c)]
        self.same = tl is full_tl
        self.by_line = {c["line"]: c for c in self.cut if c.get("line") is not None}

    def find(self, text, scene=None, prefer="back"):
        at = " ".join(text.split())
        hit = next((c for c in self.full if " ".join(c["text"].split()).startswith(at)
                    and (not scene or c["scene"] == scene)), None)
        if hit is None:
            return None, None
        if self.same:
            return hit, "exact"
        c = self.by_line.get(hit.get("line"))
        if c is not None:
            return c, "exact"
        same = [c for c in self.cut if c["scene"] == hit["scene"] and c.get("line") is not None]
        before = [c for c in same if c["line"] < hit["line"]]
        after = [c for c in same if c["line"] > hit["line"]]
        order = (before[-1:] + after[:1]) if prefer == "back" else (after[:1] + before[-1:])
        return (order[0], prefer) if order else (None, None)


MIN_SHARED_BEAT = 1.6     # listener cut: frames whose lines were dropped share a beat, at least this long each


def schedule(board, tl, full_tl, listener=False):
    """Frame timing for one board (shared by assemble and tools/beatmap.py). Returns in_range,
    t_start, t_end, frames, starts, hits; `frames` can be shorter than the board in the listener cut."""
    first_scene, last_scene = board["scenes"]
    in_range = [c for c in tl if first_scene <= c["scene"] <= last_scene]
    # The audiobook plays a scene chime 0.40 s + 0.61 s + 0.40 s before every heading clip (voices.py
    # stitch): an act starts on its chime, and the next act's chime is not ours.
    t_start = 0.0 if first_scene == 1 else in_range[0]["start"] - CHIME_LEAD
    nxt = next((c for c in tl if c["scene"] > last_scene), None)
    t_end = (nxt["start"] - CHIME_LEAD) if nxt else in_range[-1]["end"] + 0.5

    # frame -> start time, by text-prefix anchor against the timeline (like directions.json)
    anchors = Anchors(tl, full_tl, first_scene, last_scene)
    frames, starts, hits, how = list(board["frames"]), [], [], []
    for f in frames:
        hit, kind = anchors.find(f["at"], f.get("scene"), prefer="back")
        if hit is None:
            sys.exit(f"{f['id']}: anchor not found in timeline: {' '.join(f['at'].split())!r}")
        # `offset` (seconds into the anchored line): a second drawing for the same long line,
        # e.g. after two narration paragraphs were merged into one. Clamped inside the line.
        off = float(f.get("offset", 0) or 0)
        if off:
            off = min(off, max(0.0, hit["end"] - hit["start"] - 1.0))
        starts.append(hit["start"] + off); hits.append(hit); how.append(kind)
    starts[0] = t_start
    # A scene's first frame comes up WITH the chime, not three seconds later on the first action line
    # (first viewer note, 6:04 into the first cut): snap it to chime time when it anchors on the heading
    # or the line right after it.
    heads = {c["scene"]: c["start"] for c in in_range if c["kind"] == "scene"}
    seen = set()
    for i, f in enumerate(frames):
        sc = f.get("scene")
        if i == 0 or sc in seen or sc not in heads:
            seen.add(sc); continue
        seen.add(sc)
        k = next(j for j, c in enumerate(in_range) if c["scene"] == sc)
        if starts[i] <= in_range[min(k + 1, len(in_range) - 1)]["start"] + 0.01:
            starts[i] = max(heads[sc] - CHIME_LEAD, starts[i - 1] + 2.0)
    if listener:
        # frames whose lines were dropped land on the same start as the line before them: share that
        # beat in board order; when it is too short, the dropped lines' frames go first.
        keep = [True] * len(frames)
        i = 0
        while i < len(frames):
            j = i
            while j + 1 < len(frames) and abs(starts[j + 1] - starts[i]) < 0.01:
                j += 1
            if j > i:
                t0 = starts[i]
                t1 = starts[j + 1] if j + 1 < len(frames) else t_end
                group = list(range(i, j + 1))
                while len(group) > 1 and (t1 - t0) / len(group) < MIN_SHARED_BEAT:
                    k = next((g for g in group if how[g] != "exact"), group[-1])
                    group.remove(k); keep[k] = False
                for n_, g in enumerate(group):
                    starts[g] = t0 + n_ * (t1 - t0) / len(group)
            i = j + 1
        dropped = [f["id"] for f, k in zip(frames, keep) if not k]
        if dropped:
            print(f"  listener cut: {len(dropped)} frame(s) without room left out: {', '.join(dropped)}")
        frames = [f for f, k in zip(frames, keep) if k]
        starts = [x for x, k in zip(starts, keep) if k]
        hits = [x for x, k in zip(hits, keep) if k]
    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            sys.exit(f"{frames[i]['id']} starts before the previous frame; reorder the board")
    return in_range, t_start, t_end, frames, starts, hits


def cmd_assemble(args):
    board = load_board(args.board)
    audio, tl, full_tl = load_timelines(args.cut, args.listener)
    if not audio.exists():
        sys.exit(f"need {audio} (run `s2c voices` first)")
    in_range, t_start, t_end, frames, starts, _ = schedule(board, tl, full_tl, args.listener)
    board = dict(board, frames=frames)
    ends = starts[1:] + [t_end]

    work = frame_dir(board["id"]) / "clips"
    work.mkdir(exist_ok=True)
    # picture clips (push-in renders) are shared by both cuts: a beat of the same length is the same clip.
    # Segments, the joined picture, subtitles and the mix are per cut, because assemble deletes the
    # segments it did not use.
    segdir = work / "listener" if args.listener else work
    segdir.mkdir(exist_ok=True)
    fps, xd = 24, 0.6
    if args.no_gpu:
        codec = "x264"
    elif args.codec != "auto":
        codec = args.codec
    else:
        codec = ("hevc" if nvenc_available("hevc_nvenc") else
                 "nvenc" if nvenc_available("h264_nvenc") else "x264")
    enc_v = ENCODERS[codec]
    tag = {"x264": "", "nvenc": "_nvenc", "hevc": "_hevc"}[codec]   # never mix codecs in one concat
    clips, plan, clip_jobs = [], [], []
    for f, s, e in zip(board["frames"], starts, ends):
        src = pick_frame(board, f, args.model)
        dur = max(2 * xd + 0.2, e - s + xd)  # overlap so the page-turn doesn't eat the beat
        n = int(round(dur * fps))
        # `center: [x, y]` pins the push-in target when a colored wash outweighs the accent; it goes into
        # the clip name only when set, so other clips keep their cache
        pin = f.get("center")
        cx, cy = (float(pin[0]), float(pin[1])) if pin else focus_center(src)
        # `still: true` = no push-in: for runs of one drawing where only a detail changes, a fresh
        # push-in on every short beat would make the framing jump
        zoom_to = 1.0 if f.get("still") else 1.10
        sig = hashlib.sha1(src.read_bytes()).hexdigest()[:8]   # in-place fixes change the clip too
        pinned = f"-c{cx:.2f}x{cy:.2f}" if pin else ""
        out = work / f"{f['id']}__{src.stem}-{sig}-{n}f{'-still' if f.get('still') else ''}{pinned}{tag}.mp4"   # beat length is in the key: a script edit re-times beats
        if not out.exists() or args.force:
            # Ken Burns from one still: zoom 1.0 -> zoom_to over n frames, drifting toward the focus.
            vf = (f"scale={args.zoom_src}:-2,setsar=1,"
                  f"zoompan=z='1+({zoom_to}-1)*on/{n}':d={n}:"
                  f"x='max(0,min(iw-iw/zoom,iw*{cx:.3f}-iw/zoom/2))':"
                  f"y='max(0,min(ih-ih/zoom,ih*{cy:.3f}-ih/zoom/2))':s=1920x1080:fps={fps}")
            clip_jobs.append((["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", vf,
                               "-t", f"{dur:.3f}", "-r", str(fps)] + enc_v + [str(out.with_suffix(".part.mp4"))], out))
        plan.append((f, s, e, src, cx, cy))
        clips.append((out, dur, f.get("turn")))
    from concurrent.futures import ThreadPoolExecutor

    def _enc(job):
        cmd, dst = job
        subprocess.run(cmd, check=True)
        dst.with_suffix(".part.mp4").replace(dst)     # never leave a half-written file behind
    if clip_jobs:
        print(f"  rendering {len(clip_jobs)} clips ({codec}, zoompan from {args.zoom_src}px) ...")
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            list(ex.map(_enc, clip_jobs))
    for f, s, e, src, cx, cy in plan:
        print(f"  {f['id']}  {s:7.1f}s -> {e:7.1f}s  {src.name}  focus@({cx:.2f},{cy:.2f})")

    # subtitles: every dialogue line in range, "Name: line" with the name in bold accent gold;
    # --subs all adds narration and headings in italics (for muted autoplay on social feeds)
    ass = segdir / "subs.ass"
    head = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{SUB_FONT},{SUB_SIZE},&H00FFFFFF,&H000000FF,&H00202020,&H80000000,0,0,0,0,100,100,0,0,1,"
        f"{SUB_OUTLINE},0,2,60,60,{SUB_MARGIN_V},1",
        f"Style: Narr,{SUB_FONT},{SUB_SIZE - 6},&H00F0F0F0,&H000000FF,&H00202020,&H80000000,0,1,0,0,100,100,0,0,1,"
        f"{SUB_OUTLINE},0,2,60,60,{SUB_MARGIN_V},1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines, k = [], 0
    for c in in_range:
        if args.subs == "none" or (c["kind"] != "dialogue" and args.subs != "all"):
            continue
        a, b = c["start"] - t_start, c["end"] - t_start + 0.25
        if a < 0 or c["end"] > t_end or not c["text"]:
            continue
        if c["kind"] == "dialogue":
            who = ass_escape(speaker_name(c["speaker"]))
            txt = ass_escape(clean_line(c["text"]))
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,,0,0,0,,"
                         f"{{{BS}b1{BS}c&H{SUB_NAME_COLOR}&}}{who}:{{{BS}r}} {txt}")
            k += 1
            continue
        # narration: a paragraph split at sentence ends, each piece timed by its share of the characters
        pieces = narration_pieces(clean_line(c["text"]))
        total_chars = sum(len(x) for x in pieces) or 1
        t = a
        for i, piece in enumerate(pieces):
            d = (c["end"] - c["start"]) * len(piece) / total_chars
            e = t + d + (0.25 if i == len(pieces) - 1 else 0.0)
            lines.append(f"Dialogue: 0,{ass_time(t)},{ass_time(e)},Narr,,0,0,0,,{ass_escape(piece)}")
            t += d
            k += 1
    ass.write_text(NL_.join(head + lines) + NL_, encoding="utf-8")

    # Bodies + pairwise page-turns as small parallel encodes, joined losslessly, then ONE linear
    # pass for subtitles + audio. (A single N-input xfade graph keeps every clip in memory;
    # it stalled at 29 inputs and would never survive a 400-frame board.)
    from concurrent.futures import ThreadPoolExecutor
    enc = ["-r", str(fps)] + enc_v
    jobs, segs, n = [], [], len(clips)
    # Frame-exact grid: frame k becomes visible on output frame G[k] = its anchor on the 24 fps
    # grid, and every body/turn segment is cut by frame count to land exactly there. Cutting by seconds
    # (-ss/-t) rounded every 0.6 s turn up to 15 frames and the picture drifted ~0.8 s behind the audio
    # by the end of a 50-minute act -- invisible on long beats, but a reaction shot landed a line late.
    XF = int(round(xd * fps))                                         # transition slot, frames
    G = [0] + [int(round((s0 - t_start) * fps)) for s0 in starts[1:]]
    G_end = int(round((t_end - t_start) * fps))
    pad = "tpad=stop_mode=clone:stop=96,"                            # a clip a frame short holds its last frame
    for i, (p, dur, _) in enumerate(clips):
        L = (G[i + 1] if i < n - 1 else G_end) - G[i]                 # this frame's slot on the grid
        lo = 0 if i == 0 else XF
        if L <= lo:
            sys.exit(f"{board['frames'][i]['id']}: beat of {L} frames is shorter than the {XF}-frame transition; "
                     f"merge it with a neighbour or move its anchor")
        body = segdir / f"seg_{p.stem}_f{lo}-{L}_body.mp4"   # no index: inserting a frame must not rename every later segment
        if not body.exists() or args.force:
            jobs.append((["ffmpeg", "-y", "-loglevel", "error", "-i", str(p),
                          "-vf", f"{pad}trim=start_frame={lo}:end_frame={L},setpts=PTS-STARTPTS",
                          "-frames:v", str(L - lo)] + enc + [str(body.with_suffix(".part.mp4"))], body))
        segs.append(body)
        if i < n - 1:
            q, kind = clips[i + 1][0], clips[i + 1][2]
            # the transition INTO the next frame: the page-turn by default; a frame's `turn: dissolve`
            # swaps over 0.25 s and `turn: cut` cuts -- all fill the same XF-frame slot, so timing is unchanged
            xf, xdur = {"dissolve": ("fade", 0.25), "cut": ("fade", 1 / fps)}.get(kind, (args.transition, XF / fps))
            turn = segdir / f"seg_{p.stem}_f{L}__{q.stem}{'_' + kind if kind else ''}_turn.mp4"
            if not turn.exists() or args.force:
                jobs.append((["ffmpeg", "-y", "-loglevel", "error", "-i", str(p), "-i", str(q),
                              "-filter_complex",
                              f"[0:v]{pad}trim=start_frame={L}:end_frame={L + XF},setpts=PTS-STARTPTS[a];"
                              f"[1:v]{pad}trim=end_frame={XF},setpts=PTS-STARTPTS[b];"
                              f"[a][b]xfade=transition={xf}:duration={xdur:.4f}:offset=0[v]",
                              "-map", "[v]", "-frames:v", str(XF)] + enc + [str(turn.with_suffix(".part.mp4"))], turn))
            segs.append(turn)
    print(f"  encoding {len(jobs)} segments ({len(segs)} total) ...")
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        list(ex.map(_enc, jobs))
    keep = {sg.name for sg in segs}                                    # segments from earlier cuts are dead weight
    for old_seg in segdir.glob("seg_*.mp4"):
        if old_seg.name not in keep:
            old_seg.unlink()
    lst = segdir / "concat.txt"
    lst.write_text(NL_.join(f"file '{sg.name}'" for sg in segs), encoding="utf-8")
    joined = segdir / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "concat.txt",
                    "-c", "copy", str(joined)], check=True, cwd=str(segdir))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Output naming: one file per board, named by its `output` field (act-1 / act-2 / act-3); boards
    # without it fall back to NN-<board>.mp4 (NN = `seq`) or <board>.mp4.
    seq = board.get("seq")
    if board.get("output"):
        final = OUT_DIR / f"{board['output']}.mp4"
    else:
        final = OUT_DIR / (f"{int(seq):02d}-{board['id']}.mp4" if seq is not None else f"{board['id']}.mp4")
    if args.listener:
        final = final.with_name(f"{final.stem}-listener.mp4")
    mixed = None if args.no_music else music_mix(board, tl, t_start, t_end, audio, segdir, full_tl)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", "joined.mp4",
                    *(["-i", str(mixed)] if mixed else ["-ss", f"{t_start:.3f}", "-to", f"{t_end:.3f}", "-i", str(audio)]),
                    "-vf", "subtitles=subs.ass", "-map", "0:v", "-map", "1:a",
                    *enc_v,
                    "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(final.with_suffix(".part.mp4"))],
                   check=True, cwd=str(segdir))
    # Write beside the target and swap in at the end, so a half-written file is never
    # at the output path (a player opened mid-encode would otherwise see a broken MP4).
    final.with_suffix(".part.mp4").replace(final)
    print(f"assembled {final}  ({t_end - t_start:.0f}s, {len(clips)} frames, {k} subtitle lines)")


def music_mix(board, tl, t_start, t_end, voice, work, full_tl=None):
    """Background music. Cues in music.yaml place looped themes by scene range;
    the bed is ducked under every voice line (sidechain on the narration itself), mixed with the
    voice and loudness-normalised. Returns the mixed wav, or None when no cue touches this board."""
    if not MUSIC_CATALOG.exists():
        return None
    cat = yaml.safe_load(MUSIC_CATALOG.read_text(encoding="utf-8")) or {}
    first = {}
    for c in tl:
        first.setdefault(c["scene"], c["start"])
    last = {c["scene"]: c["end"] for c in tl}
    cues = []
    for cue in cat.get("cues") or []:
        a, b = cue["scenes"] if isinstance(cue["scenes"], list) else (cue["scenes"], cue["scenes"])
        if a not in first or b not in last:
            continue
        s0, s1 = max(first[a], t_start), min(last[b], t_end)
        if s1 - s0 < 2:
            continue
        wav = MUSIC_DIR / f"{cue['theme']}.wav"
        if not wav.exists():
            sys.exit(f"music theme {cue['theme']} not generated: run `s2c music` (or assemble --no-music)")
        cues.append((wav, s0 - t_start, s1 - s0, float(cue.get("gain_db", -10)), float(cue.get("fade_in", 3)), float(cue.get("fade_out", 5))))
    sfx = []
    if SFX_CATALOG.exists():
        scat = yaml.safe_load(SFX_CATALOG.read_text(encoding="utf-8")) or {}
        lo, hi = board["scenes"]
        anchors = Anchors(tl, full_tl if full_tl is not None else tl, lo, hi)
        def anchor(text, scene=None):
            c, _ = anchors.find(text, scene, prefer="forward")
            return c if c is not None and t_start - 1 <= c["start"] <= t_end else None
        for cue in scat.get("cues") or []:
            c = anchor(cue["at"], cue.get("scene"))
            if c is None:
                continue
            wav = SFX_DIR / f"{cue['fx']}.wav"
            if not wav.exists():
                sys.exit(f"sound effect {cue['fx']} not generated: run `s2c sfx`")
            s0 = c["start"] + float(cue.get("offset", 0))
            if cue.get("until"):                                     # ambience: runs until another line starts
                u = anchor(cue["until"], cue.get("until_scene"))
                if u is None:
                    continue
                dur = u["start"] - s0
            else:
                dur = float(cue.get("dur") or subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
                                                              capture_output=True, text=True).stdout.strip())
            if dur <= 0.3 or s0 >= t_end:
                continue
            sfx.append((wav, s0 - t_start, min(dur, t_end - s0), float(cue.get("gain_db", -6)), float(cue.get("fade_in", 0.05)), float(cue.get("fade_out", 0.5)), bool(cue.get("loop", False))))
    if not cues and not sfx:
        return None
    total = t_end - t_start
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t_start:.3f}", "-to", f"{t_end:.3f}", "-i", str(voice)]
    parts = ["[0:a]aformat=sample_rates=48000:channel_layouts=stereo,apad=whole_dur=%.3f,atrim=0:%.3f[v]" % (total, total)]
    for i, (wav, at, dur, gain, fi, fo) in enumerate(cues, 1):
        cmd += ["-stream_loop", "-1", "-i", str(wav)]
        parts.append(f"[{i}:a]atrim=0:{dur:.3f},asetpts=N/SR/TB,afade=t=in:d={fi},afade=t=out:st={max(0.0, dur - fo):.3f}:d={fo},"
                     f"volume={gain}dB,adelay={int(at * 1000)}|{int(at * 1000)},apad=whole_dur={total:.3f},atrim=0:{total:.3f}[c{i}]")
    ins = "".join(f"[c{i}]" for i in range(1, len(cues) + 1))
    if cues:
        parts.append(f"{ins}amix=inputs={len(cues)}:normalize=0:duration=longest[bed]" if len(cues) > 1 else "[c1]anull[bed]")
    else:
        parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.3f}[bed]")
    k0 = len(cues) + 1
    for j, (wav, at, dur, gain, fi, fo, loop) in enumerate(sfx):
        i = k0 + j
        cmd += (["-stream_loop", "-1"] if loop else []) + ["-i", str(wav)]
        parts.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{dur:.3f},asetpts=N/SR/TB,afade=t=in:d={fi},"
                     f"afade=t=out:st={max(0.0, dur - fo):.3f}:d={fo},volume={gain}dB,adelay={int(at * 1000)}|{int(at * 1000)},"
                     f"apad=whole_dur={total:.3f},atrim=0:{total:.3f}[s{j}]")
    if sfx:
        sins = "".join(f"[s{j}]" for j in range(len(sfx)))
        parts.append(f"{sins}amix=inputs={len(sfx)}:normalize=0:duration=longest[fx]" if len(sfx) > 1 else "[s0]anull[fx]")
    # duck: the voice drives a compressor on the bed (~9 dB down under speech, back up in the gaps).
    # threshold 0.02 / ratio 8 was wrong: narration is never silent, so the bed sat 15 dB down for good
    # and there was no audible music at all.
    parts.append("[v]asplit[v1][v2];[bed][v2]sidechaincompress=threshold=0.1:ratio=3:attack=60:release=1200[bd];"
                 + ("[v1][bd][fx]amix=inputs=3:normalize=0:duration=first[mix]" if sfx else "[v1][bd]amix=inputs=2:normalize=0:duration=first[mix]"))   # no loudnorm: acts with and without music must match
    out = work / "mix.wav"
    subprocess.run(cmd + ["-filter_complex", ";".join(parts), "-map", "[mix]", "-ar", "48000", str(out)], check=True)
    print(f"  audio: {len(cues)} music cue(s) ducked under the voice, {len(sfx)} sound effect(s) -> {out.name}")
    return out


def cmd_edit(args):
    """Small in-place edit of a frame's picked image: the image is the only reference and
    the prompt says to change nothing but the named element. Result = a new candidate."""
    board = load_board(args.board)
    f = next((x for x in board["frames"] if x["id"] == args.frame), None)
    if f is None:
        sys.exit(f"no frame {args.frame} in {args.board}")
    src = pick_frame(board, f, args.source_model)
    url = upload(src)
    prompt = ("EDIT this image. Change ONLY the following, and keep everything else exactly as it is "
              "(same drawing, same ink line style, same composition, same faces, same lettering): "
              + args.instruction.strip() + " Do not add any text, caption or watermark.")
    mk = args.model
    out_url, raw = generate(mk, prompt, [url], None)
    n = next_index(board["id"], f["id"], mk)
    dest = frame_dir(board["id"]) / f"{f['id']}-{mk}-{n}.png"
    dest.write_bytes(requests.get(out_url, timeout=300).content)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as lf:
        lf.write(json.dumps({"t": int(time.time()), "board": board["id"], "frame": f["id"], "model": MODELS[mk]["id"],
                             "cost_usd": MODELS[mk]["price"], "file": dest.name, "prediction": raw.get("id"),
                             "edit": args.instruction.strip()[:120]}) + chr(10))
    print(f"  {f['id']} edited from {src.name} -> {dest.name}  (${MODELS[mk]['price']:.3f})")


def cmd_merge(args):
    """Join finished board videos losslessly (concat demuxer, stream copy) into one
    deliverable, e.g. `merge feature act-1 act-2 act-3`. Inputs must share codec,
    size, frame rate and audio format, which every board assembled here does."""
    srcs = [OUT_DIR / (n if n.endswith(".mp4") else n + ".mp4") for n in args.inputs]
    for sp in srcs:
        if not sp.exists():
            sys.exit(f"missing {sp}")
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(sp)],
                               capture_output=True, text=True)
        if probe.returncode != 0 or not probe.stdout.strip():
            sys.exit(f"{sp.name} is not a finished MP4 (still being written?)")
    lst = OUT_DIR / f"_concat_{args.name}.txt"
    lst.write_text(NL_.join(f"file '{sp.name}'" for sp in srcs) + NL_, encoding="utf-8")
    final = OUT_DIR / (args.name if args.name.endswith(".mp4") else args.name + ".mp4")
    part = final.with_suffix(".part.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst.name,
                    "-c", "copy", "-movflags", "+faststart", str(part)], check=True, cwd=str(OUT_DIR))
    part.replace(final)
    lst.unlink(missing_ok=True)
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
                       capture_output=True, text=True).stdout.strip()
    print(f"merged {len(srcs)} videos -> {final}  ({float(d)/60:.1f} min, {final.stat().st_size/1e6:.0f} MB)")


def cmd_prune(args):
    """Delete clip/segment files the last assembly did not use. A clip is kept when its
    stem occurs inside any used segment name (clip stems contain "__", so no regex)."""
    work = frame_dir(args.board) / "clips"
    lst = work / "concat.txt"
    if not lst.exists():
        sys.exit("no concat.txt: assemble first")
    def used(path):
        return {ln.split("'")[1] for ln in path.read_text(encoding="utf-8").splitlines() if "'" in ln} if path.exists() else set()
    used_segs = used(lst)
    used_listener = used(work / "listener" / "concat.txt")       # the listener cut shares the clips
    removed = freed = kept_clips = 0
    for f in sorted((work / "listener").glob("seg_*.mp4")) if (work / "listener").exists() else []:
        if f.name not in used_listener:
            freed += f.stat().st_size
            if not args.dry_run:
                f.unlink()
            removed += 1
    used_segs = used_segs | used_listener
    for f in sorted(work.iterdir()):
        if f.suffix != ".mp4" or f.name == "joined.mp4":
            continue
        if f.name.startswith("seg_"):
            keep = f.name in used(lst)
        else:
            keep = any(f.stem in sname for sname in used_segs)
            kept_clips += keep
        if not keep:
            freed += f.stat().st_size
            if not args.dry_run:
                f.unlink()
            removed += 1
    print(f"{'would prune' if args.dry_run else 'pruned'} {removed} files ({freed/1e6:.0f} MB); "
          f"kept {kept_clips} clips + {len(used_segs)} segments")


def cmd_status(args):
    board = load_board(args.board)
    spent = 0.0
    if LEDGER.exists():
        for ln in LEDGER.read_text(encoding="utf-8").splitlines():
            e = json.loads(ln)
            if e.get("board") == board["id"]:
                spent += e.get("cost_usd", 0)
    for f in board["frames"]:
        c = candidates(board["id"], f["id"])
        print(f"  {f['id']}  {len(c)} candidate(s)  {'pick=' + f['pick'] if f.get('pick') else ''}")
    print(f"spent on this board: ${spent:.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    project.add_project_arg(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("refs", cmd_refs), ("gen", cmd_gen), ("sheet", cmd_sheet), ("fix", cmd_fix),
                     ("assemble", cmd_assemble), ("status", cmd_status), ("prune", cmd_prune), ("edit", cmd_edit), ("merge", cmd_merge)):
        p = sub.add_parser(name)
        if name == "merge":
            p.add_argument("name", help="output name, e.g. act-1")
            p.add_argument("inputs", nargs="+", help="videos in out/ in order, e.g. act-1 act-2")
            p.set_defaults(fn=fn)
            continue
        p.add_argument("board")
        p.set_defaults(fn=fn)
        if name == "gen":
            p.add_argument("--model", default="nb21d", help="comma-separated keys: " + ",".join(MODELS))
            p.add_argument("--frames", help="comma-separated frame ids (default all)")
            p.add_argument("-n", type=int, default=1, help="candidates per frame per model")
            p.add_argument("--anchor", help="style anchor image (overrides board.anchor)")
            p.add_argument("--force", action="store_true", help="generate even if candidates exist")
            p.add_argument("--seeded", action="store_true", help="deterministic seeds (nb2 only)")
            p.add_argument("--parallel", type=int, default=4, help="concurrent generations")
        if name == "sheet":
            p.add_argument("--frames", help="comma-separated frame ids (default all)")
            p.add_argument("--all", action="store_true", help="every candidate, not just the pick")
            p.add_argument("--cols", type=int, default=3)
            p.add_argument("--tile", type=int, default=640, help="tile width in px")
            p.add_argument("--name", help="suffix for the output file")
        if name == "edit":
            p.add_argument("frame")
            p.add_argument("instruction")
            p.add_argument("--model", default="nb21d", help="edit model key: " + ",".join(MODELS))
            p.add_argument("--source-model", default="nb21d", help="whose pick to start from")
        if name == "prune":
            p.add_argument("--dry-run", action="store_true")
        if name == "fix":
            p.add_argument("--model", default="nb21d")
            p.add_argument("--frames", help="comma-separated frame ids (default: every frame with post.clock)")
        if name == "assemble":
            p.add_argument("--model", default="nb21d")
            p.add_argument("--cut", help="audio cut to assemble over (default: the one *-narrated cut in out/)")
            p.add_argument("--subs", choices=["dialogue", "all", "none"], default="dialogue",
                           help="burned-in subtitles: dialogue (default), all = narration too, none")
            p.add_argument("--listener", action="store_true",
                           help="build the shorter listener cut -> out/<output>-listener.mp4 ([[skip]] lines dropped, "
                                "short headings; needs `s2c voices --only all`)")
            p.add_argument("--no-music", action="store_true", help="voice only, ignore music.yaml / sfx.yaml")
            p.add_argument("--transition", default="smoothleft",
                           help="ffmpeg xfade transition (smoothleft, coverleft, hlwind, fade ...)")
            p.add_argument("--force", action="store_true")
            p.add_argument("--codec", choices=["auto", "hevc", "nvenc", "x264"], default="auto",
                           help="auto = hevc_nvenc if present, else h264_nvenc, else libx264")
            p.add_argument("--no-gpu", action="store_true", help="force libx264 even if NVENC is available")
            p.add_argument("--zoom-src", type=int, default=2560,
                           help="upscale width fed to zoompan (3840 = sharper, ~2x slower)")
            p.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL,
                           help=f"concurrent ffmpeg encodes (default {DEFAULT_PARALLEL}: ~2.5 threads each on this CPU)")
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
