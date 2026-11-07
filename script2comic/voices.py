#!/usr/bin/env python3
"""
voices -- render a Fountain screenplay as an audiobook with ElevenLabs.

Outputs in <project>/out/:
  <name>-narrated.mp3            narrator reads scene headings + action, each character speaks
                                 their own lines; <name>-narrated.timeline.json beside it is the
                                 clock every picture, subtitle and sound cue hangs off
  <name>-dialogue.mp3            dialogue only (soft chime between scenes)
  <name>-listener.mp3            the shorter cut ([[skip]] / [[say:]] markers), with --only all

Dialogue and narration are rendered with Eleven v4 (audio tags for
expression, [pause] control, more reliable request stitching for long-form),
or narration with free Edge TTS when --narrator edge is given.

Incremental: every chunk is cached in cache/ under a hash of everything
that affects its sound (engine, model, voice, settings, text, effects).
Re-running after a script edit only synthesizes new/changed chunks and
re-stitches. ElevenLabs credits are only spent on cache misses.

Usage (from the project directory, or with --project DIR):
  s2c voices --dry-run            # what would be synthesized, credit estimate
  s2c voices                      # render narrated + dialogue cuts
  s2c voices --only narrated      # or --only dialogue / listener / all
  s2c voices --scenes 1-6         # sample: only the first six scenes
  s2c voices --narrator edge      # free Edge TTS narration, credits for dialogue only
  s2c voices --list-cast          # characters, line counts, voices
  s2c voices --dump-lines         # worksheet of every dialogue line (for directions.json)
  s2c voices --prune-cache        # delete cached audio no longer used

Casting lives in cast.json; per-line performance directions in directions.json.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

if __package__:
    from . import project
else:                                   # run as a file: python script2comic/voices.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from script2comic import project

PROJECT = project.PROJECT
CAST_FILE = PROJECT / "cast.json"
DIRECTIONS_FILE = PROJECT / "directions.json"
CACHE_DIR = project.CACHE / "voices"
OUT_DIR = project.OUT
LEDGER = PROJECT / "ledger-voices.jsonl"
DURATIONS = CACHE_DIR / "_durations.json"

API = "https://api.elevenlabs.io"
NARRATOR = "NARRATOR"

# Credits per character as the `character-cost` response header reported them on one account:
# eleven_v4 0.22 on pay-as-you-go and 0.40 on a Starter plan (measured Oct-Nov 2026), v3 / multilingual_v2 ~0.55,
# flash ~0.28. Used for --dry-run estimates only; ledger-voices.jsonl records the actual charges.
RATE_PER_CHAR = {"eleven_v4": 0.40, "eleven_v4_turbo": 0.20,
                 "eleven_v3": 0.55, "eleven_multilingual_v2": 0.55,
                 "eleven_flash_v2_5": 0.28, "eleven_turbo_v2_5": 0.28}
ESTIMATE_MARGIN = 1.10

EDGE_NARRATOR = {"voice": "en-US-ChristopherNeural", "rate": "-4%"}

# ---------------------------------------------------------------------------
# Expression: parenthetical / tone keywords -> Eleven v4 audio tags
# ---------------------------------------------------------------------------
# First matching rule wins per direction; several directions on a line stack
# (deduplicated, max 3 tags — v4 follows tag instructions more reliably
# than v3 did, so a third nuance tag no longer destabilises the read).
TAG_RULES = [
    (("whisper", "hushed", "under his breath", "under her breath",
      "barely audible", "barely a whisper", "mouths"),           "[whispers]"),
    (("shout", "yell", "scream", "bellow", "roar", "explod", "barks"), "[shouting]"),
    (("furious", "angry", "anger", "rage", "snap", "harsh", "seeth"), "[angry]"),
    (("sharp", "cutting", "clipped", "cold", "icy", "hard"),        "[coldly]"),
    (("flat", "hollow", "deadpan", "dead level", "dead", "toneless",
      "like a price list", "monotone"),                             "[deadpan]"),
    (("even", "level", "measured", "calm", "steady", "composed"),   "[calm]"),
    (("tearful", "tears", "crying", "weep", "sob", "broken", "grief",
      "voice breaks", "barely holds", "choked"),                    "[tearful]"),
    (("sad", "heavy", "mournful", "sorrow", "hurt"),                "[sad]"),
    (("quiet", "soft", "low", "gentle", "gently", "murmur", "tender",
      "almost to herself", "almost to himself", "small", "barely"), "[softly]"),
    (("urgent", "rushed", "frantic", "panick", "rapid", "fast",
      "breathless", "hurried"),                                     "[urgently]"),
    (("excited", "eager", "bright", "delighted", "thrilled", "giddy"), "[excited]"),
    (("trembling", "shaky", "afraid", "scared", "nervous", "fearful",
      "uneasy", "anxious", "hesitant", "falter", "stammer"),        "[nervously]"),
    (("warm", "kind", "reassuring", "fond", "affection"),           "[warmly]"),
    (("firm", "resolute", "command", "authority", "decisive", "final"), "[firmly]"),
    (("sarcas", "dry", "wry", "mocking", "scoff", "sneer", "acid"),  "[sarcastic]"),
    (("amused", "smiling", "smile", "teasing", "playful", "grinning",
      "grin", "sly", "wink", "light"),                              "[amused]"),
    (("laugh", "chuckl", "giggl", "snicker"),                       "[laughs]"),
    (("sigh", "exhale", "weary"),                                   "[sighs]"),
    (("weak", "faint", "exhausted", "tired", "fading", "dying",
      "drained", "spent"),                                          "[exhausted]"),
    (("mutter", "to herself", "to himself", "grumbl", "mumbl"),     "[muttering]"),
    (("awe", "wonder", "reverent", "stunned", "marvel"),            "[awestruck]"),
    (("pleading", "begging", "desperate", "imploring"),             "[pleading]"),
    (("curious", "puzzled", "wondering", "questioning"),            "[curious]"),
    (("surprised", "startled", "shocked", "alarmed", "taken aback"), "[surprised]"),
    (("bitter", "resentful"),                                       "[bitterly]"),
    (("proud", "triumphant", "swagger"),                            "[proudly]"),
    (("relieved", "relief"),                                        "[relieved]"),
    (("grave", "solemn", "serious", "sober"),                       "[gravely]"),
]
# Directions that are staging, not performance: never turned into a tag.
STAGING_WORDS = ("to the", "into the", "turning", "standing", "arriving",
                 "hooking", "tips her", "off his", "off her", "re:", "cont'd",
                 "continuing", "beat", "pause", "then", "reading", "pointing",
                 "holding", "over the", "on the", "into", "from", "at the")
# Directions that mean "processed through a channel" -> audio effect.
RADIO_WORDS = ("comm", "over the link", "over the radio", "radio", "headset",
               "on speaker", "over the phone", "on the phone", "filtered",
               "through the door", "over the intercom")
BEAT_RE = re.compile(r"^(a |long |another |a long |short )*(beat|pause|silence|then|a moment)\.?$")
MAX_TAGS = 3

# v4 accepts only these voice_settings (no style / speed / speaker boost),
# and its expressive stack keeps v3's three stability presets.
V4_SETTINGS = ("stability", "similarity_boost")
V4_STABILITY_PRESETS = (0.0, 0.5, 1.0)


def settings_for_model(model, settings):
    if not settings or not model.startswith("eleven_v4"):
        return settings
    s = {k: v for k, v in settings.items() if k in V4_SETTINGS}
    if "stability" in s:
        s["stability"] = min(V4_STABILITY_PRESETS, key=lambda p: abs(p - s["stability"]))
    return s

# All-caps tokens the narrator should still read as acronyms.
ACRONYMS = {"NYSE", "NASA", "USA", "GDP", "CEO", "CTO", "CFO", "FBI",
            "CIA", "NSA", "UN", "EU", "AI", "TV", "ELI5", "PM", "AM", "DNA",
            "GPS", "ID", "OK", "IPO", "ETF", "ATM", "PIN", "MIT", "UI", "IT"}


def _load_env():
    """ELEVENLABS_API_KEY from <project>/.env or the environment."""
    return project.env("ELEVENLABS_API_KEY", required=True)


# ---------------------------------------------------------------------------
# Fountain parsing
# ---------------------------------------------------------------------------

CUE_RE = re.compile(r"^[A-Z][A-Z0-9 .'#\-]*(\s*\(.*\))?\s*\^?$")   # '#' for CHILD #2 etc.
CUE_BLACKLIST_PREFIXES = (
    "INT", "EXT", "EST", "I/E", "FADE", "CUT", "SMASH", "DISSOLVE",
    "MATCH CUT", "BACK TO", "MONTAGE", "FLASHBACK", "TITLE", "SUPER",
    "QUICK CUT", "THE END", "FLASH:", "FLASH ",
)


def clean_text(text):
    text = re.sub(r"\[\[.*?\]\]", "", text)            # notes
    text = re.sub(r"\*+([^*]*)\*+", r"\1", text)        # bold/italic
    text = re.sub(r"_+([^_]*)_+", r"\1", text)          # underline
    text = re.sub(r"^>\s*(.*?)\s*<?$", r"\1", text)     # centered text
    text = text.replace("^", "")
    return re.sub(r"\s+", " ", text).strip()


def is_scene_heading(line):
    u = line.upper()
    if u.startswith((".", "INT.", "INT ", "EXT.", "EXT ", "EST.", "INT./EXT",
                     "EXT./INT", "I/E")):
        return True
    if u.startswith(("FLASHBACK", "MONTAGE")) and ("-" in u or "INT" in u or "EXT" in u):
        return True
    return False


def is_transition(line):
    u = line.strip().upper()
    return (u.endswith("TO:") or u in
            ("FADE IN:", "FADE OUT.", "FADE OUT:", "FADE TO BLACK.", "THE END", "END"))


def is_character_cue(line, next_line):
    s = line.strip()
    if not s or not next_line.strip():
        return False
    if not CUE_RE.match(s):
        return False
    name = cue_name(s)
    if not name or name.endswith((".", ":", "!", ",")):
        return False
    if any(name.startswith(p) for p in CUE_BLACKLIST_PREFIXES):
        return False
    if len(name.split()) > 4:
        return False
    return True


def cue_name(line):
    """'MIRA (V.O.) ^' -> 'MIRA'; 'KOFI ^' -> 'KOFI'"""
    s = re.sub(r"\s*\(.*\)\s*$", "", line.strip().rstrip("^").strip())
    s = re.sub(r"\s+", " ", s).strip()
    # CUE_ALIASES in cast.json folds spellings a draft drifted through ("YOUNGER POLLY" -> "YOUNG POLLY")
    return CUE_ALIASES.get(s, s)


def cue_extension(line):
    """'MIRA (V.O.)' -> 'v.o.'"""
    m = re.search(r"\(([^)]*)\)\s*\^?$", line.strip())
    return m.group(1).strip().lower() if m else ""


SPEAK_INT_EXT = False     # set from cast.json defaults.speak_int_ext (default: off)
CUE_ALIASES = {}          # set from cast.json defaults.cue_aliases, optional


def scene_heading_to_speech(line):
    s = line.strip().lstrip(".")
    if not SPEAK_INT_EXT:
        s = re.sub(r"^(INT\.?/EXT\.?|EXT\.?/INT\.?|INT\.?|EXT\.?|EST\.?|I/E)\s+", "", s, flags=re.I)
    s = re.sub(r"^FLASHBACK\s*[-–—]?\s*", "Flashback. ", s, flags=re.I)
    s = re.sub(r"^MONTAGE\s*[-–—]?\s*", "Montage. ", s, flags=re.I)
    s = re.sub(r"\bINT\.?/EXT\.?\s*", "Interior, exterior. ", s, flags=re.I)
    s = re.sub(r"\bEXT\.?/INT\.?\s*", "Exterior, interior. ", s, flags=re.I)
    s = re.sub(r"\bINT\.?\s+", "Interior. ", s, flags=re.I)
    s = re.sub(r"\bEXT\.?\s+", "Exterior. ", s, flags=re.I)
    s = re.sub(r"\s+[-–—]+\s+", ". ", s)
    s = re.sub(r"\((.*?)\)", r". \1", s)
    pieces = []
    for piece in s.split(". "):
        piece = piece.strip(" .")
        if piece and piece.isupper():
            piece = " ".join(w.capitalize() if w not in ACRONYMS else w
                             for w in piece.lower().split()) if piece not in ACRONYMS else piece
            piece = " ".join(w if w.upper() not in ACRONYMS else w.upper() for w in piece.split())
        pieces.append(piece)
    s = ". ".join(p for p in pieces if p)
    return s.rstrip(".") + "."


def narration_text(text):
    """Screenplay caps (GREEN NOTEBOOK, character intros) -> normal case for
    the narrator, so the TTS doesn't shout them. Acronyms are kept."""
    def fix(m):
        w = m.group(0)
        core = re.sub(r"[^A-Z]", "", w)
        if core in ACRONYMS or len(core) < 3:
            return w
        return w.capitalize() if "'" not in w else w[0] + w[1:].lower()
    text = re.sub(r"\b[A-Z][A-Z'’\-]{2,}\b", fix, text)
    text = text.replace("--", " — ")
    return text


def parse_fountain(path):
    """Return (title, elements). Each element is a dict:
       kind: scene | action | dialogue
       speaker, text, exprs (lowercase parentheticals), ext (cue extension),
       scene (1-based scene number), line (source line number)"""
    raw = path.read_text(encoding="utf-8")
    raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)     # boneyard
    lines = raw.splitlines()

    title = None
    start = 0
    if lines and re.match(r"^[A-Za-z ]+:\s*\S", lines[0]):
        for i, ln in enumerate(lines):
            if not ln.strip():
                start = i
                break
            m = re.match(r"^Title:\s*(.+)$", ln, flags=re.I)
            if m:
                title = clean_text(m.group(1))

    elements = []
    scene_no = 0
    i, n = start, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s or s.startswith(("#", "=", "~", "!")) and not s.startswith("!"):
            i += 1
            continue
        if s.startswith("!"):                               # forced action
            s = s[1:].strip()
        nxt = lines[i + 1] if i + 1 < n else ""

        if is_scene_heading(s):
            scene_no += 1
            say = SAY_RE.search(s)
            elements.append(dict(kind="scene", speaker=NARRATOR,
                                 text=scene_heading_to_speech(clean_text(s)),
                                 exprs=[], ext="", scene=scene_no, line=i + 1,
                                 heading=clean_text(s).lstrip("."),
                                 say=say.group(1).strip() if say else None))
            i += 1
        elif is_transition(s):
            i += 1
        elif is_character_cue(s, nxt):
            speaker, ext = cue_name(s), cue_extension(s)
            src = i + 1
            i += 1
            spoken, exprs = [], []
            while i < n and lines[i].strip():
                d = lines[i].strip()
                if d.startswith("(") and d.endswith(")"):
                    inner = d[1:-1].strip().lower()
                    if BEAT_RE.match(inner):
                        spoken.append("...")
                    else:
                        exprs.append(inner)
                else:
                    spoken.append(clean_text(d))
                i += 1
            text = " ".join(t for t in spoken if t).strip()
            text = re.sub(r"\s*\.\.\.\s*\.\.\.", " ...", text)
            if text.replace(".", "").strip():
                elements.append(dict(kind="dialogue", speaker=speaker, text=text,
                                     exprs=exprs, ext=ext, scene=scene_no, line=src))
        else:
            para = []
            while i < n and lines[i].strip():
                para.append(lines[i].strip())
                i += 1
            raw_para = " ".join(para)
            text = clean_text(raw_para)
            if text:
                elements.append(dict(kind="action", speaker=NARRATOR,
                                     text=narration_text(text), exprs=[], ext="",
                                     scene=scene_no, line=i - len(para) + 1,
                                     skip=bool(SKIP_RE.search(raw_para)),
                                     say=(m.group(1).strip() if (m := SAY_RE.search(raw_para)) else None)))
    return title, elements


# ---------------------------------------------------------------------------
# Performance directions (directions.json)
# ---------------------------------------------------------------------------
# directions.json entries:
#   {"character": "MIRA", "match": "Where did you find that?",
#    "tags": "[softly]"}                       -> tags prefixed to the line
#   {"character": "KOFI", "match": "I turned her around.",
#    "text": "[low, tired] I turned her... around."}   -> full replacement text
#   {"character": "RADIO VOICE", "match": "...", "fx": "radio"}   -> audio effect
# `match` is compared whitespace-normalised; a prefix of >= 12 chars also
# matches so small trailing edits don't orphan a direction.

def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def load_directions(*paths):
    entries = []
    for p in paths:
        if p and p.exists():
            for e in json.loads(p.read_text(encoding="utf-8")):
                if e.get("character") == "_":
                    continue                              # readme entry
                e = dict(e)
                e["_src"] = p.name
                entries.append(e)
    by_speaker = {}
    for e in entries:
        by_speaker.setdefault(e["character"].upper(), []).append(
            dict(e, match=_norm(e["match"]), used=False))
    return by_speaker


def apply_directions(elements, by_speaker):
    prev_text = ""
    for el in elements:
        if el["kind"] != "dialogue":
            prev_text = el["text"]
            continue
        for cand in by_speaker.get(el["speaker"], ()):
            if not (el["text"] == cand["match"] or (
                    len(cand["match"]) >= 12 and el["text"].startswith(cand["match"]))):
                continue
            # Optional disambiguators for lines that recur (memories, flashes):
            #   "after": substring of the preceding action/heading text
            #   "scene": scene number
            if cand.get("after") and cand["after"].lower() not in prev_text.lower():
                continue
            if cand.get("scene") and cand["scene"] != el["scene"]:
                continue
            if True:
                cand["used"] = True
                if cand.get("tone"):                    # older files: a one-word tone instead of tags
                    el["exprs"] = el["exprs"] + [cand["tone"].lower()]
                if cand.get("tags"):
                    el.setdefault("tags", []).extend(
                        t.strip() for t in re.findall(r"\[[^\]]+\]", cand["tags"]))
                if cand.get("text"):
                    el["override"] = cand["text"]
                if cand.get("fx"):
                    el["fx"] = cand["fx"]
    stale = [c for cands in by_speaker.values() for c in cands if not c["used"]]
    return stale


def exprs_to_tags(exprs):
    """Map parenthetical / tone directions to v3 tags (plus a radio flag)."""
    tags, radio = [], False
    for expr in exprs:
        e = expr.lower()
        if any(w in e for w in RADIO_WORDS):
            radio = True
        for keywords, tag in TAG_RULES:
            if any(_kw(k, e) for k in keywords):
                if tag not in tags:
                    tags.append(tag)
                break
    return tags, radio


def _kw(keyword, expr):
    """Keyword match on word boundaries ('weep' must not match 'sweeping';
    'kind' must not match 'unkind'). Keywords may be stems: 'chuckl' matches
    'chuckles', 'panick' matches 'panicked'."""
    return re.search(r"(?<![a-z])" + re.escape(keyword), expr) is not None


def _words(text):
    """Comparable form of a line: tags, ellipses, punctuation and case removed."""
    text = re.sub(r"\[[^\]]+\]", " ", text)
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()


OUTDATED_OVERRIDES = []


def dialogue_text(el):
    """Final text sent to v3 for a dialogue element."""
    if el.get("override"):
        if _words(el["override"]) == _words(el["text"]):
            return el["override"]
        # The line was reworded since the override was written: speak the
        # current words, keep only the override's leading tags.
        OUTDATED_OVERRIDES.append((el["speaker"], el["text"][:60]))
        lead = re.match(r"^\s*((?:\[[^\]]+\]\s*)+)", el["override"])
        if lead:
            el.setdefault("tags", [])
            el["tags"] = re.findall(r"\[[^\]]+\]", lead.group(1)) + el["tags"]
    tags, radio = exprs_to_tags(el["exprs"])
    for t in el.get("tags", []):                       # explicit directions win
        if t not in tags:
            tags.insert(0, t)
    if radio and not el.get("fx"):
        el["fx"] = "radio"
    ext = el.get("ext", "")
    if ("v.o." in ext or "o.s." in ext) and not el.get("fx"):
        el["fx"] = "offscreen"
    tags = tags[:MAX_TAGS]
    text = el["text"]
    return (" ".join(tags) + " " + text).strip() if tags else text


# ---------------------------------------------------------------------------
# Casting
# ---------------------------------------------------------------------------

def load_cast():
    if not CAST_FILE.exists():
        sys.exit(f"missing {CAST_FILE}")
    cast = json.loads(CAST_FILE.read_text(encoding="utf-8"))
    return cast


def voice_spec(cast, speaker):
    """Resolve a speaker to a concrete voice spec, following 'alias'."""
    spec = cast["characters"].get(speaker)
    if spec is None:
        return None
    seen = set()
    out = {}
    while spec is not None and "alias" in spec and spec["alias"] not in seen:
        seen.add(spec["alias"])
        base = cast["characters"].get(spec["alias"], {})
        merged = dict(base)
        merged.update({k: v for k, v in spec.items() if k != "alias"})
        spec = merged
        if "alias" in base and base["alias"] not in seen:
            spec["alias"] = base["alias"]
        else:
            spec.pop("alias", None)
    out.update(spec)
    return out


def assign_missing(cast, speakers):
    """Give any new character an unused voice from the pool; persist."""
    changed = False
    used = {v.get("voice_id") for v in cast["characters"].values() if v.get("voice_id")}
    for sp in speakers:
        if sp in cast["characters"]:
            continue
        pick = None
        for p in cast.get("pool", []):
            if p["voice_id"] not in used:
                pick = p
                break
        if pick is None:
            pick = cast["pool"][0] if cast.get("pool") else {"voice_id": cast["characters"][NARRATOR]["voice_id"], "name": "narrator"}
        used.add(pick["voice_id"])
        cast["characters"][sp] = {"voice_id": pick["voice_id"], "name": pick["name"],
                                  "note": "auto-assigned; recast in cast.json if wrong"}
        print(f"  new character {sp!r}: auto-assigned {pick['name']}")
        changed = True
    if changed:
        CAST_FILE.write_text(json.dumps(cast, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# ElevenLabs client
# ---------------------------------------------------------------------------

class Eleven:
    def __init__(self, key):
        self.h = {"xi-api-key": key}
        self.spent = 0
        self.lock = __import__("threading").Lock()

    def subscription(self):
        r = requests.get(f"{API}/v1/user/subscription", headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json()

    def library_voice_ids(self):
        ids, page = set(), None
        while True:
            params = {"page_size": 100}
            if page:
                params["next_page_token"] = page
            r = requests.get(f"{API}/v2/voices", headers=self.h, params=params, timeout=30)
            r.raise_for_status()
            j = r.json()
            ids |= {v["voice_id"] for v in j.get("voices", [])}
            page = j.get("next_page_token")
            if not page or not j.get("has_more"):
                break
        return ids

    def add_shared_voice(self, owner, voice_id, name):
        r = requests.post(f"{API}/v1/voices/add/{owner}/{voice_id}", headers=self.h,
                          json={"new_name": name}, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"could not add voice {name}: {r.status_code} {r.text[:200]}")
        return r.json().get("voice_id", voice_id)

    def ensure_voices(self, cast, needed):
        have = self.library_voice_ids()
        for sp in needed:
            spec = voice_spec(cast, sp)
            if not spec or spec["voice_id"] in have:
                continue
            if spec.get("public_owner_id"):
                print(f"  adding library voice {spec['name']} for {sp}")
                self.add_shared_voice(spec["public_owner_id"], spec["voice_id"], spec["name"])
                have.add(spec["voice_id"])
            else:
                print(f"  WARNING: voice {spec['voice_id']} ({spec.get('name')}) for {sp} "
                      f"is not in your library and has no public_owner_id")

    def tts(self, text, voice_id, model, settings, prev=None, nxt=None, seed=None):
        body = {"text": text, "model_id": model}
        if settings:
            body["voice_settings"] = settings
        if prev:
            body["previous_text"] = prev[-600:]
        if nxt:
            body["next_text"] = nxt[:600]
        if seed is not None:
            body["seed"] = seed
        url = f"{API}/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
        delay = 3
        for attempt in range(6):
            r = requests.post(url, headers={**self.h, "Content-Type": "application/json"},
                              json=body, timeout=180)
            if r.status_code == 200:
                cost = int(r.headers.get("character-cost", "0") or 0)
                with self.lock:
                    self.spent += cost
                    with LEDGER.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"t": int(time.time()), "model": model,
                                            "voice": voice_id, "chars": len(text),
                                            "cost": cost, "rid": r.headers.get("request-id")}) + "\n")
                return r.content
            # 409 already_running: a voice add/delete for this voice is still
            # settling and the API asks us to retry shortly.
            if r.status_code in (409, 429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(delay)
                delay = min(delay * 2, 40)
                continue
            raise RuntimeError(f"TTS {r.status_code} for voice {voice_id}: {r.text[:300]}")


# ---------------------------------------------------------------------------
# Chunks, cache keys, post-processing
# ---------------------------------------------------------------------------

def build_chunks(elements, cast, narrator_engine, title):
    """Turn parsed elements into renderable chunks (one per synthesized clip)."""
    chunks = []
    if title:
        chunks.append(dict(kind="title", speaker=NARRATOR, text=f"{title}.", scene=0))
    for el in elements:
        c = dict(kind=el["kind"], speaker=el["speaker"], scene=el["scene"],
                 line=el.get("line"), heading=el.get("heading"),
                 skip=el.get("skip", False), say=el.get("say"))
        if el["kind"] == "dialogue":
            c["text"] = dialogue_text(el)
            c["plain"] = el["text"]
            c["fx"] = el.get("fx")
        else:
            c["text"] = el["text"]
        chunks.append(c)

    for c in chunks:
        spec = voice_spec(cast, c["speaker"]) or voice_spec(cast, NARRATOR)
        if spec is None:
            sys.exit(f"cast.json needs a {NARRATOR} entry (and one per speaking character)")
        if c["speaker"] == NARRATOR:
            if narrator_engine == "edge":
                c["engine"] = "edge"
                c["voice"] = dict(EDGE_NARRATOR)
                c["model"] = "edge"
            else:
                c["engine"] = "eleven"
                c["voice"] = spec["voice_id"]
                c["model"] = spec.get("model", cast["defaults"]["narration_model"])
                c["settings"] = settings_for_model(
                    c["model"], spec.get("settings", cast["defaults"]["narration_settings"]))
        else:
            c["engine"] = "eleven"
            c["voice"] = spec["voice_id"]
            c["model"] = spec.get("model", cast["defaults"]["dialogue_model"])
            c["settings"] = settings_for_model(
                c["model"], spec.get("settings", cast["defaults"]["dialogue_settings"]))
            # v4 has an explicit pause control; standalone '...' tokens are
            # parsed screenplay beats (attached ones like 'Just...' stay).
            if c["model"].startswith("eleven_v4"):
                c["text"] = " ".join("[pause]" if w == "..." else w
                                     for w in c["text"].split())
        c["pitch"] = spec.get("pitch_semitones", 0)
        c["tempo"] = float(spec.get("tempo", 1.0))
        c["fx"] = c.get("fx") or spec.get("fx")
        set_key(c)
    return chunks


def set_key(c):
    """Cache key = everything that changes the audio. Neighbour text is NOT part of it, so dropping a
    line from a cut never invalidates the clips around it."""
    key_parts = [c["engine"], str(c["model"]), json.dumps(c["voice"], sort_keys=True),
                 json.dumps(c.get("settings", {}), sort_keys=True), str(c["pitch"]),
                 str(c.get("fx") or ""), c["text"]]
    if abs(c["tempo"] - 1.0) > 0.005:
        key_parts.append(f"tempo={c['tempo']:.3f}")   # keeps existing cache keys valid
    c["key"] = hashlib.sha1("|".join(key_parts).encode("utf-8")).hexdigest()
    c["path"] = CACHE_DIR / f"{c['key']}.mp3"


# ---------------------------------------------------------------------------
# Listener cut: the same script, shorter, for people who want the story rather than a
# script review. Both cuts render side by side (--only all): the full narrated cut is untouched.
# Action paragraphs marked [[skip]] are dropped and scene headings shrink to what changed since the
# previous one. Notes are already stripped from spoken text (clean_text) so the full cut never hears
# them, and the cache key ignores neighbours, so every surviving clip is reused: only shortened
# headings cost credits. Markers, written in the script:
#   [[skip]] Kofi flinches. Hands up.           action paragraph left out of the listener cut
#   [[say: Mira runs for it.]] She takes the pier at a run, past the ...
#                                                action paragraph spoken as the shorter text instead
#   INT. HARBOR OFFICE - NIGHT [[say: Later.]]   heading spoken exactly as given in the listener cut
#   INT. HARBOR OFFICE - NIGHT [[say:]]          chime only
# ---------------------------------------------------------------------------
SKIP_RE = re.compile(r"\[\[\s*skip\s*\]\]", re.I)
SAY_RE = re.compile(r"\[\[\s*say\s*:(.*?)\]\]", re.I)
HEADING_TIMES = {"MORNING", "AFTERNOON", "EVENING", "NIGHT", "DAY", "LATE NIGHT", "DUSK", "DAWN",
                 "CONTINUOUS", "SAME", "MOMENTS LATER", "DAYS PASSING"}
HEADING_MODES = ("FLASHBACK", "MEMORY", "NIGHTMARE", "MONTAGE", "EPILOGUE")
RECENT_HEADINGS = 4    # a place named this recently is not repeated ("War Room.", not "The Campus. War Room.")


def parse_heading(h):
    s = re.sub(r"^(INT\.?/EXT\.?|EXT\.?/INT\.?|INT\.?|EXT\.?|EST\.?|I/E)\s+", "", h.strip(), flags=re.I)
    tags = [t.strip().upper() for t in re.findall(r"\(([^)]*)\)", s)]
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    pieces = [x.strip() for x in re.split(r"\s+[-\u2013\u2014]+\s+", s) if x.strip()]
    time = pieces.pop().upper() if pieces and pieces[-1].upper() in HEADING_TIMES else None
    return dict(loc=pieces, time=time, tags=tags)


def _say(piece):
    return scene_heading_to_speech(piece).rstrip(".") + "."


def listener_heading(info, st):
    """What changed since the previous heading, as one short spoken line ('' = chime only).
    `st` carries the previous heading's place, time and mode between calls."""
    lead, tail = [], []
    mode = None
    for t in info["tags"]:
        first = t.split(" - ")[0].strip()
        if first in HEADING_MODES:
            mode = first
            if first == "EPILOGUE":
                if not st["epilogue"]:
                    lead.append(_say(t))
                    st["epilogue"] = True
            elif st["mode"] != first:
                lead.append(" ".join(_say(x) for x in t.split(" - ")))
        elif first == "PRESENT":
            if st["mode"] in ("FLASHBACK", "MEMORY", "NIGHTMARE"):
                lead.append("Present.")
        else:                                    # LATER, WEEKS LATER: a jump in time, always worth saying
            tail.append(_say(t))
    loc, prev = info["loc"], st["loc"]
    up = [x.upper() for x in loc]
    prev_up = {x.upper() for x in prev}
    same_place = bool(loc) and set(up) <= prev_up      # same place, or a named part of it
    if st["first"]:
        new = list(loc)
    elif not loc or same_place:
        new = []
    else:
        new = [x for x in loc if x.upper() not in prev_up]
        recent = set().union(*st["recent"]) if st["recent"] else set()
        fresh = [x for x in new if x.upper() not in recent]
        new = fresh or new[-1:]
    time = info["time"]
    if time == "DAYS PASSING":
        tail.append(_say(time))
    elif time and time not in ("CONTINUOUS", "SAME") and (st["first"] or (not new and time != st["time"])):
        tail.append(_say(time))
    text = " ".join(lead + [_say(x) for x in new] + tail)
    if loc and not same_place:
        st["loc"] = loc
    if time and time not in ("CONTINUOUS", "SAME"):
        st["time"] = time
    st["mode"] = mode
    st["recent"] = (st["recent"] + [set(up)])[-RECENT_HEADINGS:]
    st["first"] = False
    return text


def listener_chunks(chunks):
    """The listener cut's chunk list, derived from the full one (same objects where unchanged)."""
    st = dict(loc=[], time=None, mode=None, recent=[], first=True, epilogue=False)
    out = []
    for c in chunks:
        if c["kind"] == "action" and (c.get("skip") or c.get("say") == ""):
            continue
        if c["kind"] == "action" and c.get("say"):
            d = dict(c, text=narration_text(clean_text(c["say"])))
            set_key(d)
            out.append(d)
            continue
        if c["kind"] == "scene":
            auto = listener_heading(parse_heading(c.get("heading") or ""), st)
            text = c["say"] if c.get("say") is not None else auto
            if not text:
                out.append(dict(c, silent=True, text="", path=None))
            elif text == c["text"]:
                out.append(c)
            else:
                d = dict(c, text=text)
                set_key(d)
                out.append(d)
            continue
        out.append(c)
    return out


def ffmpeg_filter(pitch, fx, tempo=1.0):
    filters = []
    if pitch:
        f = 2 ** (pitch / 12.0)
        filters.append(f"asetrate=44100*{f:.6f},aresample=44100,atempo={1 / f:.6f}")
    if tempo and abs(tempo - 1.0) > 0.005:
        filters.append(f"atempo={max(0.5, min(2.0, tempo)):.3f}")
    if fx == "radio":
        filters.append("highpass=f=300,lowpass=f=3400,acompressor=threshold=-18dB:ratio=3,volume=1.2")
    elif fx == "offscreen":
        filters.append("lowpass=f=6000,volume=0.85")
    elif fx == "ethereal":
        filters.append("aecho=0.7:0.6:60|120:0.35|0.2,lowpass=f=5000")
    elif fx == "abyss":
        # ominous voice from somewhere deep below: long cavernous echo,
        # bass-heavy, muffled highs, a little slower
        filters.append("atempo=0.93,highpass=f=90,lowpass=f=2400,"
                       "aecho=0.8:0.85:140|290|470:0.55|0.4|0.25,"
                       "bass=g=6:f=150,volume=1.1")
    elif fx == "distant":
        filters.append("lowpass=f=4000,volume=0.7,aecho=0.6:0.4:40:0.3")
    # trim leading/trailing silence, leave a hair of room
    filters.append("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.12")
    filters.append("areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.20,areverse")
    return ",".join(filters)


TARGET_MEAN_DB = -20.0      # per-clip loudness target (mean volume)
GAIN_RANGE_DB = (-7.0, 9.0)  # cap the correction so whispers stay quieter than shouts


def mean_volume_db(path):
    r = subprocess.run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    m = re.search(r"mean_volume: ([-\d.]+) dB", r)
    return float(m.group(1)) if m else None


def normalize_gain(in_path, out_path):
    """Second pass: bring the clip's mean volume toward TARGET_MEAN_DB (within
    GAIN_RANGE_DB) and limit peaks, then encode to mono 44.1k mp3."""
    mean = mean_volume_db(in_path)
    gain = 0.0 if mean is None else max(GAIN_RANGE_DB[0], min(GAIN_RANGE_DB[1], TARGET_MEAN_DB - mean))
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(in_path),
         "-af", f"volume={gain:.1f}dB,alimiter=limit=0.89:attack=5:release=50",
         "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", str(out_path)],
        check=True)


def postprocess(raw_path, out_path, pitch, fx, tempo=1.0):
    """Every clip: pitch/tempo/effects + silence trim (pass 1, to WAV), then
    loudness normalisation and mp3 encode (pass 2)."""
    tmp = out_path.with_suffix(f".{os.getpid()}.{__import__('threading').get_ident()}.tmp.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_path),
         "-af", ffmpeg_filter(pitch, fx, tempo), "-ar", "44100", "-ac", "1", str(tmp)], check=True)
    normalize_gain(tmp, out_path)
    tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

def neighbours(chunks, idx):
    """Previous/next same-speaker text within the same scene (for continuity).
    v4's request stitching is reliable enough to use for dialogue too, so a
    character's performance carries across their lines in a scene."""
    c = chunks[idx]
    prev = nxt = None
    for j in range(idx - 1, -1, -1):
        if chunks[j]["scene"] != c["scene"]:
            break
        if chunks[j]["speaker"] == c["speaker"]:
            prev = chunks[j]["text"]
            break
    for j in range(idx + 1, len(chunks)):
        if chunks[j]["scene"] != c["scene"]:
            break
        if chunks[j]["speaker"] == c["speaker"]:
            nxt = chunks[j]["text"]
            break
    return prev, nxt


def synth_eleven(client, chunks, todo_idx, concurrency, budget):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    total = len(todo_idx)
    done = 0

    def work(i):
        c = chunks[i]
        prev, nxt = neighbours(chunks, i)
        seed = int(c["key"][:8], 16) % 4294967295
        audio = client.tts(c["text"], c["voice"], c["model"], c.get("settings"),
                           prev, nxt, seed)
        raw = c["path"].with_suffix(f".{__import__('threading').get_ident()}.raw.mp3")
        raw.write_bytes(audio)
        postprocess(raw, c["path"], c["pitch"], c.get("fx"), c.get("tempo", 1.0))
        raw.unlink(missing_ok=True)
        return i

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {}
        pending = list(todo_idx)
        while pending or futures:
            while pending and len(futures) < concurrency:
                i = pending.pop(0)
                futures[ex.submit(work, i)] = i
            for fut in as_completed(list(futures)):
                i = futures.pop(fut)
                fut.result()
                done += 1
                if done % 20 == 0 or done == total:
                    print(f"  synthesized {done}/{total}  (credits this run: {client.spent})")
                if budget and client.spent > budget:
                    print(f"  budget of {budget} credits exceeded; stopping (cache keeps what's done)")
                    pending.clear()
                break


async def _edge_one(text, voice, out_path, sem):
    import edge_tts
    kwargs = {k: voice[k] for k in ("rate", "pitch", "volume") if voice.get(k)}
    async with sem:
        for attempt in range(4):
            try:
                tmp = out_path.with_suffix(".raw.mp3")
                await edge_tts.Communicate(text, voice["voice"], **kwargs).save(str(tmp))
                postprocess(tmp, out_path, 0, None)
                tmp.unlink(missing_ok=True)
                return
            except Exception as e:                    # noqa: BLE001
                if attempt == 3:
                    raise RuntimeError(f"edge-tts failed: {e}") from e
                await asyncio.sleep(2 * (attempt + 1))


async def synth_edge(chunks, todo_idx, concurrency=8):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    await asyncio.gather(*(_edge_one(chunks[i]["text"], chunks[i]["voice"], chunks[i]["path"], sem)
                           for i in todo_idx))
    print(f"  edge-tts synthesized {len(todo_idx)} narration chunks")


# ---------------------------------------------------------------------------
# Stitching
# ---------------------------------------------------------------------------

def make_silence(seconds, path):
    if not path.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "anullsrc=r=44100:cl=mono", "-t", str(seconds), "-c:a", "libmp3lame",
                        "-b:a", "128k", str(path)], check=True)
    return path


def make_chime(path):
    """Short, soft scene-change cue (~0.6 s): two quick notes, fast decay.
    Drop a file named scene_cue.mp3 (or .wav) in the project directory to use
    your own sound instead."""
    custom = next((PROJECT / f"scene_cue{ext}" for ext in (".mp3", ".wav") if (PROJECT / f"scene_cue{ext}").exists()), None)
    if custom is not None:
        if not path.exists() or path.stat().st_mtime < custom.stat().st_mtime:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(custom), "-ar", "44100",
                            "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", str(path)], check=True)
        return path
    if not path.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=f=880:d=0.45",
             "-f", "lavfi", "-i", "sine=f=1320:d=0.35",
             "-filter_complex",
             "[0]afade=t=in:d=0.005,afade=t=out:st=0.05:d=0.40:curve=exp,volume=0.9[a];"
             "[1]afade=t=in:d=0.005,afade=t=out:st=0.03:d=0.32:curve=exp,adelay=110,volume=0.6[b];"
             "[a][b]amix=inputs=2:normalize=0,apad=pad_dur=0.15,aresample=44100",
             "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", str(path)], check=True)
    return path


def duration_of(path, cache):
    k = path.name
    if k in cache:
        return cache[k]
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        d = float(p.stdout.strip())
    except ValueError:
        d = 0.0
    cache[k] = d
    return d


def stitch(chunks, output, narrated, chime=True):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    gap = {
        "after_dialogue": make_silence(0.40, CACHE_DIR / "_sil_040.mp3"),
        "after_action": make_silence(0.55, CACHE_DIR / "_sil_055.mp3"),
        "after_heading": make_silence(0.70, CACHE_DIR / "_sil_070.mp3"),
        "before_scene": make_silence(1.30, CACHE_DIR / "_sil_130.mp3"),
        "after_title": make_silence(1.80, CACHE_DIR / "_sil_180.mp3"),
    }
    marker = make_chime(CACHE_DIR / "_cue.mp3") if chime else gap["before_scene"]

    durations = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
    entries, timeline, clips, t = [], [], [], 0.0
    first = True
    for c in chunks:
        if c["kind"] == "scene":
            if not narrated:
                if not first:
                    for piece in (gap["after_dialogue"], marker, gap["after_dialogue"]):
                        entries.append(piece)
                        t += duration_of(piece, durations)
                timeline.append((t, c.get("heading") or c["text"]))
                first = False
                continue
            if not first:
                if chime:
                    for piece in (gap["after_dialogue"], marker, gap["after_dialogue"]):
                        entries.append(piece)
                        t += duration_of(piece, durations)
                else:
                    entries.append(gap["before_scene"])
                    t += duration_of(gap["before_scene"], durations)
            timeline.append((t, c.get("heading") or c["text"]))
            first = False
            if c.get("silent"):                  # listener cut: chime only, nothing spoken
                clips.append({"start": round(t, 3), "end": round(t, 3), "kind": "scene", "speaker": c["speaker"],
                              "scene": c["scene"], "line": c.get("line"), "heading": c.get("heading"), "text": ""})
                continue
        if c["kind"] in ("action", "title") and not narrated:
            continue
        start = t
        entries.append(c["path"])
        t += duration_of(c["path"], durations)
        clips.append({"start": round(start, 3), "end": round(t, 3), "kind": c["kind"],
                      "speaker": c["speaker"], "scene": c["scene"], "line": c.get("line"),
                      "heading": c.get("heading"), "text": c.get("plain") or c["text"]})
        g = {"dialogue": "after_dialogue", "action": "after_action",
             "scene": "after_heading", "title": "after_title"}[c["kind"]]
        entries.append(gap[g])
        t += duration_of(gap[g], durations)
    DURATIONS.write_text(json.dumps(durations))

    list_file = CACHE_DIR / f"_concat_{output.stem}.txt"
    list_file.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in entries),
                         encoding="utf-8")
    print(f"  stitching {sum(1 for e in entries if not e.name.startswith('_'))} clips -> {output}")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(list_file), "-c:a", "libmp3lame", "-b:a", "128k",
                    "-metadata", f"title={output.stem.replace('-', ' ').title()}",
                    str(output)], check=True)
    # Exact per-clip clock of this cut (for visuals / subtitles synced to the narration).
    output.with_suffix(".timeline.json").write_text(
        json.dumps(clips, ensure_ascii=False, indent=0), encoding="utf-8")
    with output.with_suffix(".scenes.txt").open("w", encoding="utf-8") as f:
        for secs, heading in timeline:
            f.write(f"{int(secs // 3600):d}:{int(secs % 3600 // 60):02d}:{int(secs % 60):02d}  {heading}\n")
    print(f"  done: {output}  ({int(t // 3600)}h {int(t % 3600 // 60)}m {int(t % 60)}s), "
          f"scene index in {output.with_suffix('.scenes.txt').name}")


def prune_cache(keep):
    keep_names = {c["path"].name for c in keep}
    removed = 0
    for f in CACHE_DIR.glob("*.mp3"):
        if f.name not in keep_names and not f.name.startswith("_"):
            f.unlink()
            removed += 1
    print(f"  pruned {removed} stale cache files")


# ---------------------------------------------------------------------------
# Re-matching directions after a rewrite
# ---------------------------------------------------------------------------

def rematch_directions(elements, paths, apply=False, threshold=0.55):
    """For every entry in the given direction files that no longer matches a
    line, find the closest current line by the same character (difflib) and
    re-point its `match` at it. Entries whose line was cut are dropped.
    A full-text `text` override written for the old wording is reduced to
    its leading tags, since the words changed. Prints every decision;
    writes the files only with apply=True."""
    import difflib
    dialogue = [e for e in elements if e["kind"] == "dialogue"]
    for path in paths:
        if not path or not path.exists():
            continue
        entries = json.loads(path.read_text(encoding="utf-8"))
        by_speaker = {}
        for e in entries:
            if e.get("character") == "_":
                continue
            by_speaker.setdefault(e["character"].upper(), []).append(
                dict(e, match=_norm(e["match"]), used=False))
        apply_directions(elements, by_speaker)
        used_lines = set()
        for cands in by_speaker.values():
            for c in cands:
                if c["used"]:
                    for el in dialogue:
                        if el["speaker"] == c["character"].upper() and (
                                el["text"] == c["match"] or el["text"].startswith(c["match"])):
                            used_lines.add(id(el))
        kept, remapped, dropped = [], 0, 0
        stale = [c for cands in by_speaker.values() for c in cands if not c["used"]]
        stale_keys = {(c["character"].upper(), c["match"]) for c in stale}
        print(f"\n{path.name}: {len(stale)} stale of {len(entries)}")
        for e in entries:
            if e.get("character") == "_":
                kept.append(e)
                continue
            key = (e["character"].upper(), _norm(e["match"]))
            if key not in stale_keys:
                kept.append(e)
                continue
            pool = [el for el in dialogue if el["speaker"] == key[0] and id(el) not in used_lines]
            best, score = None, 0.0
            for el in pool:
                r = difflib.SequenceMatcher(None, key[1].lower(), el["text"].lower()).ratio()
                # also compare against the head of a long line
                r2 = difflib.SequenceMatcher(None, key[1].lower(), el["text"][:len(key[1]) + 10].lower()).ratio()
                r = max(r, r2)
                if r > score:
                    best, score = el, r
            if best is None or score < threshold:
                dropped += 1
                print(f"  DROP   {key[0]}: \"{key[1][:60]}\"  (best {score:.2f})")
                continue
            used_lines.add(id(best))
            new = dict(e)
            t = best["text"]
            new["match"] = t if len(t) <= 60 else t[:max(12, t[:60].rfind(" "))]
            if new.get("text"):
                tags = " ".join(re.findall(r"^\s*((?:\[[^\]]+\]\s*)+)", new["text"])[:1]).strip()
                new.pop("text")
                if tags:
                    new["tags"] = (new.get("tags", "") + " " + tags).strip()
                new["_note"] = "inline-tag text dropped after rewrite; re-add if wanted"
            remapped += 1
            print(f"  REMAP  {key[0]}: \"{key[1][:48]}\" -> \"{new['match'][:48]}\"  ({score:.2f})")
            kept.append(new)
        print(f"  {remapped} remapped, {dropped} dropped")
        if apply:
            path.write_text(json.dumps(kept, indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"  wrote {path}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def parse_scene_range(spec, max_scene):
    if not spec:
        return None
    keep = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            keep |= set(range(int(a), int(b or max_scene) + 1))
        elif part:
            keep.add(int(part))
    return keep


def list_cast(elements, cast):
    counts, chars = {}, {}
    for el in elements:
        if el["kind"] == "dialogue":
            counts[el["speaker"]] = counts.get(el["speaker"], 0) + 1
            chars[el["speaker"]] = chars.get(el["speaker"], 0) + len(el["text"])
    print(f"{'CHARACTER':22}{'LINES':>6}{'CHARS':>7}  VOICE")
    spec = voice_spec(cast, NARRATOR)
    print(f"{NARRATOR:22}{'-':>6}{'-':>7}  {spec['name']}  [{spec.get('model', cast['defaults']['narration_model'])}]")
    for sp in sorted(counts, key=lambda s: -counts[s]):
        spec = voice_spec(cast, sp) or {}
        extra = []
        if spec.get("pitch_semitones"):
            extra.append(f"pitch {spec['pitch_semitones']:+d}st")
        if spec.get("tempo") and abs(float(spec["tempo"]) - 1.0) > 0.005:
            extra.append(f"tempo x{float(spec['tempo']):.2f}")
        if spec.get("fx"):
            extra.append(f"fx {spec['fx']}")
        print(f"{sp:22}{counts[sp]:>6}{chars[sp]:>7}  {spec.get('name', '?? UNCAST')}"
              f"{'  (' + ', '.join(extra) + ')' if extra else ''}")


def dump_lines(elements):
    """Worksheet: every dialogue line with its context, for writing directions.json."""
    prev_action = ""
    for el in elements:
        if el["kind"] == "action":
            prev_action = el["text"]
        elif el["kind"] == "scene":
            print(f"\n## scene {el['scene']}: {el.get('heading')}")
            prev_action = ""
        else:
            tags, _ = exprs_to_tags(el["exprs"])
            ctx = (prev_action[:90] + "…") if len(prev_action) > 90 else prev_action
            print(f"[{el['line']}] ({ctx})\n    {el['speaker']}{' ' + str(el['exprs']) if el['exprs'] else ''}"
                  f"{' -> ' + ' '.join(tags + el.get('tags', [])) if (tags or el.get('tags')) else ''}: {el['text']}")
            prev_action = ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    project.add_project_arg(ap)
    ap.add_argument("--script", help="screenplay (default: <project>/script.fountain or the only *.fountain there)")
    ap.add_argument("--only", choices=["narrated", "dialogue", "listener", "both", "all"], default="both",
                    help="both = narrated + dialogue; listener = the shorter [[skip]]-aware cut; all = all three")
    ap.add_argument("--narrator", choices=["eleven", "edge"], default="eleven",
                    help="engine for scene/action narration (edge = free Microsoft TTS)")
    ap.add_argument("--scenes", help="scene numbers to render, e.g. 1-6 or 3,7,10-12")
    ap.add_argument("--limit", type=int, help="only the first N chunks (quick sample)")
    ap.add_argument("--dry-run", action="store_true", help="estimate credits, synthesize nothing")
    ap.add_argument("--budget", type=int, help="abort if the estimate (or actual spend) exceeds this many credits")
    ap.add_argument("--concurrency", type=int, default=3, help="parallel ElevenLabs requests (Starter allows 3, Creator 5)")
    ap.add_argument("--chime", action=argparse.BooleanOptionalAction, default=True,
                    help="short sound cue at every scene change (--no-chime = silence)")
    ap.add_argument("--list-cast", action="store_true")
    ap.add_argument("--dump-lines", action="store_true")
    ap.add_argument("--prune-cache", action="store_true")
    ap.add_argument("--rematch-directions", choices=["preview", "apply"],
                    help="after a rewrite: re-point stale entries in directions.json "
                         "at their closest current line (preview prints, apply writes)")
    ap.add_argument("--renormalize", action="store_true",
                    help="re-apply loudness normalisation to every cached clip (no credits spent)")
    ap.add_argument("--name", help="output file stem (default: the script's title, slugified)")
    args = ap.parse_args()

    args.script = project.script_path(args.script)
    if not args.script.exists():
        sys.exit(f"script not found: {args.script}")
    if not CAST_FILE.exists():
        sys.exit(f"missing {CAST_FILE} (copy example/cast.json and edit)")
    global SPEAK_INT_EXT, CUE_ALIASES
    defaults = json.loads(CAST_FILE.read_text(encoding="utf-8")).get("defaults", {})
    SPEAK_INT_EXT = bool(defaults.get("speak_int_ext", False))
    CUE_ALIASES = {k.upper(): v.upper() for k, v in (defaults.get("cue_aliases") or {}).items()}
    title, elements = parse_fountain(args.script)
    if not args.name:
        args.name = re.sub(r"[^a-z0-9]+", "-", (title or args.script.stem).lower()).strip("-") or "script"
    if args.rematch_directions:
        rematch_directions(elements, [DIRECTIONS_FILE], apply=args.rematch_directions == "apply")
        return
    directions = load_directions(DIRECTIONS_FILE)
    stale = apply_directions(elements, directions)
    cast = load_cast()
    speakers = []
    for el in elements:
        if el["kind"] == "dialogue" and el["speaker"] not in speakers:
            speakers.append(el["speaker"])
    assign_missing(cast, speakers)

    if args.list_cast:
        list_cast(elements, cast)
        return
    if args.dump_lines:
        dump_lines(elements)
        return
    if args.renormalize:
        clips = [f for f in CACHE_DIR.glob("*.mp3") if not f.name.startswith("_")]
        for f in clips:
            tmp = f.with_suffix(".renorm.mp3")
            normalize_gain(f, tmp)
            tmp.replace(f)
        if DURATIONS.exists():
            DURATIONS.unlink()
        print(f"  renormalised {len(clips)} cached clips")

    chunks = build_chunks(elements, cast, args.narrator, title)
    max_scene = max(c["scene"] for c in chunks)
    keep = parse_scene_range(args.scenes, max_scene)
    if keep is not None:
        chunks = [c for c in chunks if c["scene"] in keep]
    if args.limit:
        chunks = chunks[:args.limit]

    want_full = args.only in ("narrated", "dialogue", "both", "all")
    want_listener = args.only in ("listener", "all")
    need_narration = args.only in ("narrated", "both", "all")
    wanted = [c for c in chunks if want_full and (need_narration or c["kind"] == "dialogue")]
    lchunks = listener_chunks(chunks) if want_listener else []
    lwanted = [c for c in lchunks if not c.get("silent")]
    # One synthesis per distinct clip: identical chunks (repeated headings,
    # repeated lines) share a cache path and must not be rendered twice at once.
    seen_paths = set()
    todo, ltodo = [], []
    for i, c in enumerate(chunks):
        if c in wanted and not c["path"].exists() and c["path"] not in seen_paths:
            seen_paths.add(c["path"])
            todo.append(i)
    for i, c in enumerate(lchunks):
        if not c.get("silent") and not c["path"].exists() and c["path"] not in seen_paths:
            seen_paths.add(c["path"])
            ltodo.append(i)
    todo_eleven = [i for i in todo if chunks[i]["engine"] == "eleven"]
    todo_edge = [i for i in todo if chunks[i]["engine"] == "edge"]
    ltodo_eleven = [i for i in ltodo if lchunks[i]["engine"] == "eleven"]
    ltodo_edge = [i for i in ltodo if lchunks[i]["engine"] == "edge"]

    est = 0.0
    by_model = {}
    for c in [chunks[i] for i in todo_eleven] + [lchunks[i] for i in ltodo_eleven]:
        n = len(c["text"])
        by_model[c["model"]] = by_model.get(c["model"], [0, 0])
        by_model[c["model"]][0] += 1
        by_model[c["model"]][1] += n
        est += n * RATE_PER_CHAR.get(c["model"], 1.0)
    est = math.ceil(est * ESTIMATE_MARGIN)

    print(f"audiobook: '{title}' - {len(speakers)} speaking characters, {max_scene} scenes")
    print(f"  chunks in scope: {len(wanted)}   cached: {len(wanted) - len(todo)}   "
          f"to synthesize: {len(todo_eleven)} ElevenLabs + {len(todo_edge)} edge-tts")
    if want_listener:
        skipped = sum(1 for c in chunks if c["kind"] == "action" and c.get("skip"))
        short = sum(1 for c in lchunks if c["kind"] == "scene" and not c.get("silent") and c.get("heading")
                    and c["text"] != scene_heading_to_speech(c["heading"]))
        silent = sum(1 for c in lchunks if c.get("silent"))
        print(f"  listener cut: {len(lwanted)} clips, {skipped} action paragraphs skipped, {short} headings "
              f"shortened, {silent} chime-only; to synthesize: {len(ltodo_eleven)} ElevenLabs")
    for m, (cnt, n) in by_model.items():
        print(f"    {m:24} {cnt:5} chunks  {n:7} chars  ~{math.ceil(n * RATE_PER_CHAR.get(m, 1.0) * ESTIMATE_MARGIN):6} credits")
    print(f"  estimated ElevenLabs credits: ~{est}")
    if OUTDATED_OVERRIDES:
        print(f"  WARNING: {len(OUTDATED_OVERRIDES)} directions.json 'text' overrides no longer match the "
              f"line's words; using current words + their tags. Rewrite the override or drop it:")
        for sp, t in OUTDATED_OVERRIDES[:8]:
            print(f"    {sp}: \"{t}\"")
    if stale:
        by_src = {}
        for e in stale:
            by_src.setdefault(e["_src"], []).append(e)
        for src, es in by_src.items():
            print(f"  WARNING: {len(es)} entries in {src} no longer match any line (script changed?):")
            for e in es[:6]:
                print(f"    {e['character']}: \"{e['match'][:60]}\"")

    key = _load_env()
    client = Eleven(key)
    try:
        sub = client.subscription()
        remaining = sub["character_limit"] - sub["character_count"]
        print(f"  account: {sub['tier']} tier, {remaining:,} of {sub['character_limit']:,} credits remaining")
        if est > remaining:
            print(f"  WARNING: estimate exceeds remaining credits. Consider --narrator edge or --scenes.")
    except Exception as e:                            # noqa: BLE001
        print(f"  (could not read subscription: {e})")

    if args.dry_run:
        return
    if args.budget and est > args.budget:
        sys.exit(f"estimate {est} exceeds --budget {args.budget}; nothing synthesized")

    if todo_eleven:
        client.ensure_voices(cast, {chunks[i]["speaker"] for i in todo_eleven})
        synth_eleven(client, chunks, todo_eleven, args.concurrency, args.budget)
    if ltodo_eleven:
        client.ensure_voices(cast, {lchunks[i]["speaker"] for i in ltodo_eleven})
        synth_eleven(client, lchunks, ltodo_eleven, args.concurrency, args.budget)
    if todo_eleven or ltodo_eleven:
        print(f"  ElevenLabs credits charged this run: {client.spent}")
    if todo_edge:
        asyncio.run(synth_edge(chunks, todo_edge))
    if ltodo_edge:
        asyncio.run(synth_edge(lchunks, ltodo_edge))

    missing = [c for c in wanted + lwanted if not c["path"].exists()]
    if missing:
        print(f"  {len(missing)} chunks still missing (budget stop or errors); not stitching")
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if keep is None and not args.limit else "-sample"
    if args.only in ("narrated", "both", "all"):
        stitch(chunks, OUT_DIR / f"{args.name}-narrated{suffix}.mp3", narrated=True, chime=args.chime)
    if args.only in ("dialogue", "both", "all"):
        stitch(chunks, OUT_DIR / f"{args.name}-dialogue{suffix}.mp3", narrated=False, chime=args.chime)
    if want_listener:
        stitch(lchunks, OUT_DIR / f"{args.name}-listener{suffix}.mp3", narrated=True, chime=args.chime)
    if args.prune_cache:
        full = build_chunks(elements, cast, args.narrator, title)
        prune_cache(full + [c for c in listener_chunks(full) if not c.get("silent")])


if __name__ == "__main__":
    main()
