#!/usr/bin/env python3
"""Render a standalone voice-over (a pitch narration, a trailer line) with ElevenLabs.

    s2c voiceover pitch/pitch.json              # primary voice
    s2c voiceover pitch/pitch.json --all        # primary + alternates
    s2c voiceover <job.json> --voice Eric       # one alternate (name, prefix or voice_id)
    s2c voiceover <job.json> --dry-run          # chars + credit estimate only
    s2c voiceover <job.json> --force            # re-render even if unchanged
    s2c voiceover --list-voices                 # voices in the account

The job file stores everything needed to regenerate: text file, voice (name + voice_id), model,
voice settings, output format, split mode. Every render writes <out_dir>/<basename>--<voice>.mp3,
an .srt built from ElevenLabs' character timestamps (for burned-in subtitles), an optional
loudness-normalised .wav, and a .json sidecar with the text hash, credits charged and duration.
Each request is appended to <project>/ledger-voices.jsonl. An unchanged job (same text, voice,
model, settings) is skipped unless --force. See example/pitch/pitch.json.
"""
import argparse, base64, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

import requests

if __package__:
    from . import project
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from script2comic import project
API = "https://api.elevenlabs.io"
LEDGER = project.PROJECT / "ledger-voices.jsonl"
RATE_PER_CHAR = 0.40          # eleven_v4 credits per character (0.22 pay-as-you-go, 0.40 Starter; estimates only)
V4_STABILITY_PRESETS = (0.0, 0.5, 1.0)
TAG_RE = re.compile(r"\[[^\]]{1,40}\]")   # v4 audio tags such as [pause] [softly]


def api_key():
    return project.env("ELEVENLABS_API_KEY", required=True)


def v4_settings(settings, model):
    """v4 accepts only stability (snapped to 0 / 0.5 / 1) and similarity_boost."""
    s = dict(settings or {})
    if model.startswith("eleven_v4"):
        s = {k: v for k, v in s.items() if k in ("stability", "similarity_boost")}
        if "stability" in s:
            s["stability"] = min(V4_STABILITY_PRESETS, key=lambda p: abs(p - s["stability"]))
    return s


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.split(" - ")[0].lower()).strip("-") or "voice"


def load_job(path):
    job = json.loads(Path(path).read_text(encoding="utf-8"))
    base = Path(path).resolve().parent
    job["_text_path"] = (base / job["text_file"]).resolve()
    job["_out_dir"] = (base / job["out_dir"]).resolve() if job.get("out_dir") else project.OUT
    job["_text"] = job["_text_path"].read_text(encoding="utf-8-sig").strip()
    return job


def job_hash(job, voice):
    key = json.dumps({"model": job["model"], "voice": voice["voice_id"],
                      "settings": v4_settings(job.get("settings"), job["model"]),
                      "fmt": job.get("output_format", "mp3_44100_128"),
                      "split": job.get("split", "whole"),
                      "gap": job.get("paragraph_gap_ms", 0) if job.get("split") == "paragraphs" else 0,
                      "text": job["_text"]}, sort_keys=True)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------

def tts(key, text, voice_id, model, settings, fmt, prev=None, nxt=None):
    """One synthesis request via the with-timestamps endpoint.

    Returns (audio_bytes, cost, request_id, alignment) where alignment is the
    character-level timing dict ElevenLabs returns (or None)."""
    body = {"text": text, "model_id": model}
    if settings:
        body["voice_settings"] = settings
    if prev:
        body["previous_text"] = prev[-600:]
    if nxt:
        body["next_text"] = nxt[:600]
    url = f"{API}/v1/text-to-speech/{voice_id}/with-timestamps?output_format={fmt}"
    delay = 3
    for attempt in range(6):
        r = requests.post(url, headers={"xi-api-key": key, "Content-Type": "application/json"},
                          json=body, timeout=240)
        if r.status_code == 200:
            cost = int(r.headers.get("character-cost", "0") or 0)
            rid = r.headers.get("request-id")
            j = r.json()
            with LEDGER.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": int(time.time()), "model": model, "voice": voice_id,
                                    "chars": len(text), "cost": cost, "rid": rid, "job": "pitch_vo"}) + "\n")
            return base64.b64decode(j["audio_base64"]), cost, rid, j.get("alignment")
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 5:
            time.sleep(delay)
            delay = min(delay * 2, 40)
            continue
        raise RuntimeError(f"TTS {r.status_code} for voice {voice_id}: {r.text[:300]}")


def list_voices(key):
    r = requests.get(f"{API}/v1/voices", headers={"xi-api-key": key}, timeout=30)
    r.raise_for_status()
    for v in r.json().get("voices", []):
        lb = v.get("labels") or {}
        if True:
            print(f"{v['voice_id']:24} {v['name'][:44]:44} {v.get('category', ''):12} "
                  f"{lb.get('gender', '')}/{lb.get('age', '')}/{lb.get('use_case', '')}/{lb.get('descriptive', '')}")


def credits_left(key):
    s = requests.get(f"{API}/v1/user/subscription", headers={"xi-api-key": key}, timeout=30).json()
    return s.get("tier"), s.get("character_limit", 0) - s.get("character_count", 0)


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
        return round(float(out.strip()), 2)
    except Exception:
        return None


def concat_with_gaps(clips, gap_ms, out_mp3):
    """Decode each clip, pad it with gap_ms of silence, concatenate, encode once."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wavs = []
        for i, clip in enumerate(clips):
            w = td / f"{i:03}.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip),
                            "-af", f"apad=pad_dur={gap_ms / 1000:.3f}", "-ar", "44100", "-ac", "1", str(w)], check=True)
            wavs.append(w)
        lst = td / "list.txt"
        lst.write_text("".join(f"file '{w.as_posix()}'\n" for w in wavs), encoding="utf-8")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c:a", "libmp3lame", "-b:a", "128k", str(out_mp3)], check=True)


def loudnorm(src, dst):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000", "-ac", "1",
                    "-c:a", "pcm_s16le", str(dst)], check=True)


# ---------------------------------------------------------------------------
# Subtitles from the character alignment
# ---------------------------------------------------------------------------

def cue_spans(text, max_chars=84):
    """Yield (start_idx, end_idx) character spans of the text, one per subtitle cue:
    sentence by sentence, long sentences broken at commas/semicolons."""
    for m in re.finditer(r"[^.!?\n]+[.!?]*[\"'”’)]*\s*", text):
        s, e = m.start(), m.end()
        while e > s and text[e - 1].isspace():
            e -= 1
        if e <= s or not text[s:e].strip():
            continue
        if e - s <= max_chars:
            yield s, e
            continue
        # long sentence: split into near-equal pieces at the clause break nearest each target point
        pieces = -(-(e - s) // max_chars)
        cur = s
        for k in range(pieces - 1):
            target = s + (e - s) * (k + 1) // pieces
            lo, hi = cur + 12, e - 12
            cut = -1
            for pat in (", ", "; ", " "):
                best = -1
                for m2 in re.finditer(re.escape(pat), text[lo:hi]):
                    pos = lo + m2.start() + len(pat) - 1
                    if best < 0 or abs(pos - target) < abs(best - target):
                        best = pos
                if best >= 0 and (pat != " " or cut < 0):
                    cut = best
                    break
            if cut < 0:
                break
            yield cur, cut + 1
            cur = cut + 1
            while cur < e and text[cur].isspace():
                cur += 1
        if cur < e:
            yield cur, e


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def build_cues(text, alignment, offset=0.0):
    """Map subtitle spans of `text` onto ElevenLabs' character timings."""
    chars = alignment.get("characters") or []
    st = alignment.get("character_start_times_seconds") or []
    en = alignment.get("character_end_times_seconds") or []
    if len(chars) != len(text) or len(st) != len(chars):
        # alignment is for the text as sent; fall back to proportional timing
        total = en[-1] if en else 0.0
        st = [total * i / max(len(text), 1) for i in range(len(text))]
        en = [total * (i + 1) / max(len(text), 1) for i in range(len(text))]
    cues = []
    for s, e in cue_spans(text):
        label = TAG_RE.sub("", text[s:e]).strip()
        label = re.sub(r"\s+", " ", label)
        if not label:
            continue
        a, b = s, e - 1
        while a < e and text[a].isspace():
            a += 1
        while b > a and text[b].isspace():
            b -= 1
        cues.append((offset + st[a], offset + en[b], label))
    return cues


def write_srt(cues, path):
    lines = []
    for i, (a, b, label) in enumerate(cues, 1):
        if i < len(cues):
            b = min(b + 0.15, cues[i][0] - 0.05)   # hold a touch, never overlap the next cue
        else:
            b += 0.4
        if len(label) > 42:
            mid = len(label) // 2
            spaces = [i for i, ch in enumerate(label) if ch == " "]
            if spaces:
                cut = min(spaces, key=lambda i: abs(i - mid))
                label = label[:cut] + "\n" + label[cut + 1:]
        lines.append(f"{i}\n{srt_time(a)} --> {srt_time(max(b, a + 0.3))}\n{label}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(key, job, voice, force=False):
    text = job["_text"]
    model = job["model"]
    settings = v4_settings(job.get("settings"), model)
    fmt = job.get("output_format", "mp3_44100_128")
    out_dir = job["_out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{job.get('basename', 'voiceover')}--{slug(voice['name'])}"
    mp3 = out_dir / f"{stem}.mp3"
    side = out_dir / f"{stem}.json"
    h = job_hash(job, voice)
    if mp3.exists() and side.exists() and not force:
        try:
            if json.loads(side.read_text(encoding="utf-8")).get("hash") == h:
                print(f"  {mp3.name}: unchanged, skipped (use --force to re-render)")
                return
        except Exception:
            pass

    print(f"  rendering {voice['name']} ({voice['voice_id']}) on {model} -> {mp3.name}")
    t0 = time.time()
    spent, rids, cues = 0, [], []
    if job.get("split") == "paragraphs":
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        gap = int(job.get("paragraph_gap_ms", 700))
        with tempfile.TemporaryDirectory() as td:
            clips, offset = [], 0.0
            for i, p in enumerate(paras):
                prev = " ".join(paras[:i]) or None
                nxt = " ".join(paras[i + 1:]) or None
                audio, cost, rid, al = tts(key, p, voice["voice_id"], model, settings, fmt, prev, nxt)
                spent += cost
                rids.append(rid)
                c = Path(td) / f"{i:03}.mp3"
                c.write_bytes(audio)
                clips.append(c)
                if al:
                    cues += build_cues(p, al, offset)
                offset += (duration(c) or 0.0) + gap / 1000
                print(f"    paragraph {i + 1}/{len(paras)}: {len(p)} chars, {cost} credits")
            concat_with_gaps(clips, gap, mp3)
    else:
        audio, cost, rid, al = tts(key, text, voice["voice_id"], model, settings, fmt)
        spent, rids = cost, [rid]
        mp3.write_bytes(audio)
        if al:
            cues = build_cues(text, al)

    dur = duration(mp3)
    record = {"hash": h, "rendered_at": time.strftime("%Y-%m-%d %H:%M:%S"), "text_file": str(job["_text_path"]),
              "chars": len(text), "credits": spent, "duration_s": dur, "model": model,
              "voice": {"name": voice["name"], "voice_id": voice["voice_id"]},
              "settings": settings, "output_format": fmt, "split": job.get("split", "whole"),
              "request_ids": rids, "elapsed_s": round(time.time() - t0, 1)}
    if job.get("subtitles", True) and cues:
        srt = out_dir / f"{stem}.srt"
        try:
            write_srt(cues, srt)
            record["srt"] = srt.name
        except Exception as ex:          # never lose a paid render over a subtitle problem
            print(f"    WARNING: subtitles not written: {ex}")
    if job.get("loudnorm") and shutil.which("ffmpeg"):
        wav = out_dir / f"{stem}--norm.wav"
        loudnorm(mp3, wav)
        record["loudnorm_wav"] = wav.name
    side.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"    done: {spent} credits, {dur}s, {len(cues)} subtitle cues, sidecar {side.name}")


def pick_voice(job, wanted):
    cands = [job["voice"]] + list(job.get("alternates", []))
    w = wanted.lower()
    for v in cands:
        if v["voice_id"] == wanted or v["name"].lower() == w or v["name"].lower().startswith(w):
            return v
    if re.fullmatch(r"[A-Za-z0-9]{20}", wanted):
        return {"name": wanted, "voice_id": wanted}
    sys.exit(f"voice '{wanted}' is neither the job's voice nor one of its alternates")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    project.add_project_arg(ap)
    ap.add_argument("job", nargs="?", help="job file (see example/pitch/pitch.json)")
    ap.add_argument("--voice", help="render this voice instead (name, name prefix, or voice_id)")
    ap.add_argument("--all", action="store_true", help="render the primary voice and every alternate")
    ap.add_argument("--force", action="store_true", help="re-render even if the job is unchanged")
    ap.add_argument("--dry-run", action="store_true", help="show chars and a credit estimate, render nothing")
    ap.add_argument("--list-voices", action="store_true", help="list the account's voices")
    a = ap.parse_args()
    key = api_key()
    if a.list_voices:
        return list_voices(key)
    if not a.job:
        ap.error("a job file is required (see example/pitch/pitch.json)")
    job = load_job(a.job)
    if a.voice:
        voices = [pick_voice(job, a.voice)]
    elif a.all:
        voices = [job["voice"]] + list(job.get("alternates", []))
    else:
        voices = [job["voice"]]
    n = len(job["_text"])
    print(f"{job['_text_path'].name}: {n} chars, model {job['model']}, split={job.get('split', 'whole')}")
    print(f"estimate ~{round(n * RATE_PER_CHAR)} credits per voice x {len(voices)} voice(s)")
    if a.dry_run:
        return
    tier, left = credits_left(key)
    print(f"account: {tier} tier, {left} credits left")
    for v in voices:
        render(key, job, v, force=a.force)
    print(f"credits left: {credits_left(key)[1]}")


if __name__ == "__main__":
    main()
