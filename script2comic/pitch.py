#!/usr/bin/env python3
"""pitch -- assemble a standalone pitch video (or any short cut) from a cue list.

    s2c pitch pitch/pitch.yaml

The cue list is a flat list of SHOTS. Each shot shows one drawing (a board frame "act-two/F37t", or
an image path) with the same push-in and page-turn as the act videos, and optionally starts one piece
of audio: a cached voice clip from `s2c voices` (cast line or narration) or a voice-over passage
rendered with `s2c voiceover` (whose .json sidecar carries the ElevenLabs character timings for subtitles).

shot fields
  frame      "board/Fxx" or a path                       audio    mp3 path (or null)
  speaker    subtitle name for a cast line (NARRATOR = none)   text     spoken words (subtitle)
  vo_json    sidecar with `alignment` -> sentence-level subtitles from the timestamps
  lead       silence before the audio starts (s, default 0.3)   tail  silence after it (default 0.5)
  hold       shot length in s (default lead + audio + tail); audio may run on over later silent shots
  still      true = no push-in                           turn     page (default) | dissolve | cut  (INTO this shot)
  music      {theme, gain_db, fade_in, fade_out, seek} starts a bed here (seek = seconds into the theme), "none" stops it
  sfx        [{fx, offset, gain_db, dur, loop, fade_in, fade_out}]  placed relative to the shot start
  text_times sentence start times (s into the clip) for the subtitles of a multi-sentence line
  subtitle   false = no subtitle for this shot's audio (e.g. the title read over the title card)
Output: <project>/out/<output>.mp4 (+ .srt). Subtitles are burned in: "Name: line" for cast, plain for
narration and the voice-over. The bed is sidechain-ducked under the voice; the final mix is normalised.
Audio is placed sample-exact in numpy (voice, bed and effect stems), and only the three full-length stems
go through ffmpeg: one big adelay graph with ~50 inputs misplaced short effects at random.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

if __package__:
    from . import board as B, project, voiceover as PV
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from script2comic import board as B, project, voiceover as PV

FPS, XD = 24, 0.6
XF = int(round(XD * FPS))
SLOT = {"page": XF, "dissolve": 6, "cut": 0}
MUSIC_DIR = B.MUSIC_DIR
SFX_DIR = B.SFX_DIR
WORK = B.BOARD_CACHE / "pitch" / "clips"


def adur(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def resolve_frame(spec):
    p = Path(spec)
    if p.exists():
        return p
    bid, fid = spec.split("/", 1)
    b = B.load_board(bid)
    f = next((x for x in b["frames"] if x["id"] == fid), None)
    if f is None:
        sys.exit(f"no frame {fid} in {bid}")
    return B.pick_frame(b, f, "nb21d")




def sentence_cues(text, t0, t1, starts=None):
    """Split a cast line into sentence subtitles, timed by character share of the clip, or by `starts`
    (each sentence's start in seconds into the clip) when the shot gives `text_times`."""
    text = " ".join(text.split())
    spans = list(PV.cue_spans(text)) or [(0, len(text))]
    n = max(1, len(text)); cues = []
    for k, (s, e) in enumerate(spans):
        label = PV.TAG_RE.sub("", text[s:e]).strip()
        if not label:
            continue
        if starts and len(starts) == len(spans):
            a = t0 + starts[k]; b = t0 + starts[k + 1] if k + 1 < len(spans) else t1
        else:
            a = t0 + (t1 - t0) * s / n; b = t0 + (t1 - t0) * e / n
        cues.append((a, b, label))
    return cues


def parse_srt(path):
    """[(start, end, text)] from a .srt (times in seconds)."""
    def t(x):
        h, m, rest = x.strip().split(":"); sec, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000
    out = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a, b = lines[1].split("-->")
        out.append((t(a), t(b), " ".join(x.strip() for x in lines[2:])))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    project.add_project_arg(ap)
    ap.add_argument("cues", help="cue list YAML (see example/pitch/pitch.yaml)")
    ap.add_argument("--transition", default="smoothleft")
    ap.add_argument("--codec", choices=["auto", "hevc", "nvenc", "x264"], default="auto")
    ap.add_argument("--parallel", type=int, default=B.DEFAULT_PARALLEL)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    cues_path = Path(args.cues) if Path(args.cues).is_absolute() else (project.PROJECT / args.cues)
    if not cues_path.exists():
        cues_path = Path(args.cues)
    cue = yaml.safe_load(cues_path.read_text(encoding="utf-8"))
    base = cues_path.resolve().parent          # relative audio / vo_json / frame paths resolve against the cue file
    for s_ in cue["shots"]:
        for k in ("audio", "vo_json", "frame"):
            v = s_.get(k)
            if v and not Path(v).is_absolute() and (base / v).exists():
                s_[k] = str(base / v)
    shots = cue["shots"]
    WORK.mkdir(parents=True, exist_ok=True)

    # ---- timeline -------------------------------------------------------------------------------
    t = 0.0
    audios = []                         # (start, dur, shot)
    for s in shots:
        s["_frame"] = resolve_frame(s["frame"])     # a board frame "board/Fxx", an image, or an .mp4 to play as is
        s["_start"] = t
        lead = float(s.get("lead", 0.3)); tail = float(s.get("tail", 0.5))
        if s.get("audio"):
            d = adur(s["audio"]); s["_adur"] = d; s["_astart"] = t + lead
            dur = float(s["hold"]) if s.get("hold") else lead + d + tail
            audios.append((t + lead, d, s))
        else:
            dur = float(s["hold"])
        s["_dur"] = dur
        t += dur
    total = t
    for (a0, d0, s0), (a1, _, s1) in zip(audios, audios[1:]):
        if a0 + d0 > a1 + 0.01:
            sys.exit(f"audio of shot {shots.index(s0)} ({s0['frame']}) runs {a0 + d0 - a1:.1f}s into the next audio "
                     f"(shot {shots.index(s1)}, {s1['frame']}); lengthen a hold")
    print(f"  {len(shots)} shots, {total:.1f}s ({total / 60:.1f} min)")

    # ---- subtitles --------------------------------------------------------------------------------
    cues = []
    for a0, d, s in audios:
        if s.get("subtitle") is False:
            continue
        text = s.get("text") or ""
        if s.get("vo_json"):
            side = json.loads(Path(s["vo_json"]).read_text(encoding="utf-8"))
            if "text" in side:
                al = side.get("alignment") or {}
                if len(al.get("characters") or []) != len(side["text"]):
                    print(f"  note: alignment length differs from text for {Path(s['vo_json']).name}; timing is proportional")
                cues += [(x, y, lbl, None) for x, y, lbl in PV.build_cues(side["text"], al, offset=a0)]
            else:
                # newer pitch_vo sidecars carry no text/alignment: use the .srt it wrote beside the mp3 (ElevenLabs
                # character timings), or split the text file proportionally when there is none
                srt_p = Path(s["vo_json"]).with_name(side["srt"]) if side.get("srt") else None
                if srt_p and srt_p.exists():
                    cues += [(x + a0, y + a0, lbl, None) for x, y, lbl in parse_srt(srt_p)]
                else:
                    text = Path(side["text_file"]).read_text(encoding="utf-8-sig").strip()
                    cues += [(x, y, lbl, None) for x, y, lbl in sentence_cues(text, a0, a0 + d)]
        elif text:
            who = (s.get("speaker") or "NARRATOR").upper()
            for x, y, lbl in sentence_cues(text, a0, a0 + d, s.get("text_times")):
                cues.append((x, y, lbl, None if who == "NARRATOR" else B.speaker_name(who)))
    cues.sort(key=lambda c: (c[0], c[1]))
    ass = WORK / "subs.ass"
    head = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "WrapStyle: 0",
            "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,{B.SUB_FONT},{B.SUB_SIZE},&H00FFFFFF,&H000000FF,&H00202020,&H80000000,0,0,0,0,100,100,0,0,1,"
            f"{B.SUB_OUTLINE},0,2,60,60,{B.SUB_MARGIN_V},1", "", "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    lines, srt = [], []
    for i, (x, y, lbl, who) in enumerate(cues):
        y = min(y + 0.25, cues[i + 1][0] - 0.05) if i + 1 < len(cues) else y + 0.4
        y = max(y, x + 0.4)
        # only cast lines carry a coloured name; narration such as "The wall clock: 3:28 PM." stays plain
        txt = (f"{{{B.BS}b1{B.BS}c&H{B.SUB_NAME_COLOR}&}}{B.ass_escape(who)}:{{{B.BS}r}} {B.ass_escape(lbl)}"
               if who else B.ass_escape(lbl))
        lines.append(f"Dialogue: 0,{B.ass_time(x)},{B.ass_time(y)},Default,,0,0,0,,{txt}")
        srt.append(f"{i + 1}\n{PV.srt_time(x)} --> {PV.srt_time(y)}\n{who + ': ' if who else ''}{lbl}\n")
    ass.write_text("\n".join(head + lines) + "\n", encoding="utf-8")

    # ---- picture: one push-in clip per shot, then body/turn segments on the frame grid -----------
    codec = ("x264" if args.codec == "x264" else args.codec if args.codec != "auto" else
             "hevc" if B.nvenc_available("hevc_nvenc") else "nvenc" if B.nvenc_available("h264_nvenc") else "x264")
    enc_v = B.ENCODERS[codec]
    tag = {"x264": "", "nvenc": "_nvenc", "hevc": "_hevc"}[codec]
    from concurrent.futures import ThreadPoolExecutor

    def enc(job):
        cmd, dst = job
        subprocess.run(cmd, check=True)
        dst.with_suffix(".part.mp4").replace(dst)

    G = [int(round(s["_start"] * FPS)) for s in shots] + [int(round(total * FPS))]
    slots = [0] + [int(s["turn_frames"]) if s.get("turn_frames") else SLOT.get(s.get("turn", "page"), XF)
                   for s in shots[1:]]   # transition INTO shot i; `turn_frames` sets its length per cue
    clips, jobs = [], []
    for i, s in enumerate(shots):
        L = G[i + 1] - G[i]
        n = L + (slots[i + 1] if i + 1 < len(shots) else 0)          # this shot must also cover the turn into the next
        zoom_to = 1.0 if s.get("still") else 1.10
        cx, cy = (0.5, 0.5) if s.get("still") or s["_frame"].suffix.lower() == ".mp4" else B.focus_center(s["_frame"])
        sig = hashlib.sha1(s["_frame"].read_bytes()).hexdigest()[:8]
        clip = WORK / f"clip_{s['_frame'].stem}-{sig}-{n}f{'-still' if s.get('still') else ''}{tag}.mp4"
        if s["_frame"].suffix.lower() == ".mp4":
            # a pre-rendered moving shot (e.g. the storyboard wall): used as is, frozen on its last frame if short
            patch = None
            mono = ",hue=s=0" if s.get("mono") else ""
            if patch or mono:
                clip = clip.with_name(clip.stem + ("-mono" if mono else "") + ("-dial" if patch else "")
                                      + ("-anim" if patch and isinstance(s.get("dial"), dict) else "") + ".mp4")
            if not clip.exists() or args.force:
                if patch:
                    extra, flt = patch
                    jobs.append((["ffmpeg", "-y", "-loglevel", "error", "-i", str(s["_frame"])] + extra + ["-filter_complex",
                                  f"[0:v]scale=1920:1080,setsar=1,fps={FPS}{mono}[v0];{flt};[v1]tpad=stop_mode=clone:stop={n}[v]",
                                  "-map", "[v]", "-an", "-frames:v", str(n), "-r", str(FPS)] + enc_v
                                 + [str(clip.with_suffix(".part.mp4"))], clip))
                else:
                    jobs.append((["ffmpeg", "-y", "-loglevel", "error", "-i", str(s["_frame"]), "-an", "-vf",
                                  f"scale=1920:1080,setsar=1,fps={FPS}{mono},tpad=stop_mode=clone:stop={n}", "-frames:v", str(n),
                                  "-r", str(FPS)] + enc_v + [str(clip.with_suffix(".part.mp4"))], clip))
        elif not clip.exists() or args.force:
            vf = (f"scale=2560:-2,setsar=1,zoompan=z='1+({zoom_to}-1)*on/{n}':d={n}:"
                  f"x='max(0,min(iw-iw/zoom,iw*{cx:.3f}-iw/zoom/2))':y='max(0,min(ih-ih/zoom,ih*{cy:.3f}-ih/zoom/2))':"
                  f"s=1920x1080:fps={FPS}")
            jobs.append((["ffmpeg", "-y", "-loglevel", "error", "-i", str(s["_frame"]), "-vf", vf, "-frames:v", str(n),
                          "-r", str(FPS)] + enc_v + [str(clip.with_suffix(".part.mp4"))], clip))
        clips.append((clip, L))
    if jobs:
        print(f"  rendering {len(jobs)} push-in clips ({codec}) ...")
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            list(ex.map(enc, jobs))

    pad = "tpad=stop_mode=clone:stop=96,"
    segs, jobs = [], []
    for i, (clip, L) in enumerate(clips):
        lo = slots[i]
        if L <= lo:
            sys.exit(f"shot {i} ({shots[i]['frame']}) is {L} frames, shorter than its {lo}-frame transition")
        body = WORK / f"seg_{clip.stem}_f{lo}-{L}.mp4"
        if not body.exists() or args.force:
            jobs.append((["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip), "-vf",
                          f"{pad}trim=start_frame={lo}:end_frame={L},setpts=PTS-STARTPTS", "-frames:v", str(L - lo),
                          "-r", str(FPS)] + enc_v + [str(body.with_suffix(".part.mp4"))], body))
        segs.append(body)
        if i + 1 < len(clips) and slots[i + 1] > 0:
            nxt, kind, sl = clips[i + 1][0], shots[i + 1].get("turn", "page"), slots[i + 1]
            xf, xdur = {"dissolve": ("fade", sl / FPS)}.get(kind, (args.transition, sl / FPS))
            turn = WORK / f"seg_{clip.stem}_f{L}__{nxt.stem}_{kind}.mp4"
            if not turn.exists() or args.force:
                jobs.append((["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip), "-i", str(nxt), "-filter_complex",
                              f"[0:v]{pad}trim=start_frame={L}:end_frame={L + sl},setpts=PTS-STARTPTS[a];"
                              f"[1:v]{pad}trim=end_frame={sl},setpts=PTS-STARTPTS[b];"
                              f"[a][b]xfade=transition={xf}:duration={xdur:.4f}:offset=0[v]",
                              "-map", "[v]", "-frames:v", str(sl), "-r", str(FPS)] + enc_v + [str(turn.with_suffix(".part.mp4"))], turn))
            segs.append(turn)
    print(f"  encoding {len(jobs)} segments ({len(segs)} total) ...")
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        list(ex.map(enc, jobs))
    (WORK / "concat.txt").write_text("\n".join(f"file '{sg.name}'" for sg in segs), encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy",
                    "joined.mp4"], check=True, cwd=str(WORK))

    # ---- audio: voice + music bed (ducked) + sfx, placed sample-exact in numpy -> normalised mix -------
    import numpy as np
    SR = 48000
    N = int(round(total * SR))
    _pcm = {}

    def pcm(path):
        """Decode once to float32 stereo at 48 kHz (the same resampling and upmix ffmpeg's aformat used)."""
        key = str(path)
        if key not in _pcm:
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", key, "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                                 capture_output=True, check=True).stdout
            _pcm[key] = np.frombuffer(raw, np.float32).reshape(-1, 2)
        return _pcm[key]

    def segment(path, dur, loop, fi, fo, gain_db, seek=0.0):
        """`dur` seconds of the file from `seek` (looped if asked), linear fades like ffmpeg afade, then gain."""
        x = pcm(path); x = x[int(round(seek * SR)):] if seek else x; n = int(round(dur * SR))
        if loop and len(x) < n:
            x = np.tile(x, (n // len(x) + 1, 1))
        x = x[:n].copy(); n = len(x)
        t = np.arange(n) / SR
        g = np.ones(n, np.float32)
        if fi > 0:
            g *= np.clip(t / fi, 0, 1)
        if fo > 0:
            st = max(0.0, n / SR - fo)
            g *= np.clip(1 - (t - st) / fo, 0, 1)
        return x * g[:, None] * np.float32(10 ** (gain_db / 20))

    def place(buf, x, at):
        i = int(round(at * SR))
        if i >= len(buf):
            return
        x = x[: len(buf) - i]
        buf[i:i + len(x)] += x

    voice = np.zeros((N, 2), np.float32)
    for a0, d, s in audios:
        place(voice, pcm(s["audio"]), a0)

    # music cues: a shot's `music` starts a bed that runs until the next shot that sets music
    beds, cur = [], None
    for s in shots:
        m = s.get("music")
        if m is None:
            continue
        if cur:
            cur["end"] = s["_start"]; beds.append(cur); cur = None
        if m != "none":
            cur = dict(m, start=s["_start"])
    if cur:
        cur["end"] = total; beds.append(cur)
    bed = np.zeros((N, 2), np.float32)
    for bd in beds:
        wav = MUSIC_DIR / f"{bd['theme']}.wav"
        if not wav.exists():
            sys.exit(f"music theme {bd['theme']} not generated")
        place(bed, segment(wav, bd["end"] - bd["start"], True, float(bd.get("fade_in", 2)), float(bd.get("fade_out", 3)),
                           float(bd.get("gain_db", -10)), float(bd.get("seek", 0))), bd["start"])

    fx = np.zeros((N, 2), np.float32)
    n_fx = 0
    for s in shots:
        for e in s.get("sfx") or []:
            wav = SFX_DIR / f"{e['fx']}.wav"
            if not wav.exists():
                sys.exit(f"sound effect {e['fx']} not generated")
            at = s["_start"] + float(e.get("offset", 0))
            dur = float(e.get("dur") or adur(wav)); dur = min(dur, total - at)
            if dur <= 0.3:
                continue
            place(fx, segment(wav, dur, bool(e.get("loop")), float(e.get("fade_in", 0.05)), float(e.get("fade_out", 0.5)),
                              float(e.get("gain_db", -8))), at)
            n_fx += 1

    stems = []
    for name, arr in (("voice", voice), ("bed", bed), ("fx", fx)):
        f = WORK / f"stem_{name}.f32"
        f.write_bytes(np.ascontiguousarray(arr, np.float32).tobytes())
        stems += ["-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", str(f)]
    graph = ("[0:a]asplit[v1][v2];[1:a][v2]sidechaincompress=threshold=0.1:ratio=3:attack=60:release=1200[bd];"
             "[v1][bd][2:a]amix=inputs=3:normalize=0:duration=first[pre];"
             f"[pre]afade=t=out:st={max(0.0, total - 1.5):.3f}:d=1.5,loudnorm=I=-16:TP=-1.5:LRA=11[mix]")
    mix = WORK / "mix.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + stems + ["-filter_complex", graph, "-map", "[mix]", "-ar", "48000", str(mix)],
                   check=True)
    print(f"  audio: {len(audios)} voice clips, {len(beds)} music beds, {n_fx} sound effects")

    # ---- final ------------------------------------------------------------------------------------
    B.OUT_DIR.mkdir(exist_ok=True)
    final = B.OUT_DIR / f"{cue.get('output', 'pitch')}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", "joined.mp4", "-i", str(mix),
                    "-vf", f"subtitles=subs.ass,fade=t=in:d=0.8,fade=t=out:st={max(0.0, total - 1.5):.3f}:d=1.5",
                    "-map", "0:v", "-map", "1:a", *enc_v, "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                    str(final.with_suffix(".part.mp4"))], check=True, cwd=str(WORK))
    final.with_suffix(".srt").write_text("\n".join(srt), encoding="utf-8")   # first: a player holding the mp4 can block the swap
    final.with_suffix(".part.mp4").replace(final)
    print(f"assembled {final}  ({total:.0f}s, {len(shots)} shots, {len(cues)} subtitle cues)")


if __name__ == "__main__":
    main()
