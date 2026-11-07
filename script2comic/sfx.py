"""Generate the sound effects listed in <project>/sfx.yaml with ElevenLabs Sound Effects
(POST /v1/sound-generation), once each: an effect is re-generated only when its spec changes.
Output: <project>/sfx/<id>.mp3 (as delivered) and <id>.wav (48 kHz stereo, peak-normalised to
-3 dBFS; per-cue gain_db in the catalog sets the level in the mix). Credits read before/after.
usage: s2c sfx [--project DIR] [effect ...] [--force]"""
import hashlib, json, re, subprocess, sys
from pathlib import Path
import requests, yaml
if __package__:
    from . import project
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from script2comic import project
API = "https://api.elevenlabs.io"
CAT = project.PROJECT / "sfx.yaml"; OUT = project.PROJECT / "sfx"
PEAK_DB = -3.0

def credits(h):
    s = requests.get(f"{API}/v1/user/subscription", headers=h, timeout=30).json()
    return s["character_limit"] - s["character_count"], s["tier"]

def normalise(mp3, wav):
    o = subprocess.run(["ffmpeg", "-i", str(mp3), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    peak = float(re.search(r"max_volume: (-?[\d.]+) dB", o).group(1))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-af", f"volume={PEAK_DB - peak:.2f}dB",
                    "-ar", "48000", "-ac", "2", str(wav)], check=True)
    return peak

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]; force = "--force" in sys.argv
    h = {"xi-api-key": project.env("ELEVENLABS_API_KEY", required=True)}
    if not CAT.exists():
        sys.exit(f"no {CAT} (see example/sfx.yaml)")
    OUT.mkdir(exist_ok=True)
    fx = yaml.safe_load(CAT.read_text(encoding="utf-8"))["effects"]
    before, tier = credits(h); print(f"account: {tier}, {before:,} credits"); made = 0
    for fid, f in fx.items():
        if args and fid not in args: continue
        if f.get("from"):
            # derived effect: the same recording as another effect, run through an ffmpeg filter
            # (e.g. the pit bell heard through a wall) -- guarantees it is the SAME sound
            spec = {"from": f["from"], "filter": f["filter"]}
        else:
            spec = {"text": " ".join(f["prompt"].split()), "prompt_influence": float(f.get("influence", 0.4)), "loop": bool(f.get("loop", False))}
            if f.get("duration_s"): spec["duration_seconds"] = float(f["duration_s"])
        sig = hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]
        mp3, wav, meta = OUT / f"{fid}.mp3", OUT / f"{fid}.wav", OUT / f"{fid}.json"
        if wav.exists() and meta.exists() and json.loads(meta.read_text())["sig"] == sig and not force:
            continue
        if f.get("from"):
            src = OUT / f"{f['from']}.wav"
            if not src.exists(): print(f"  {fid}: source {src.name} missing, generate it first"); continue
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", f["filter"], str(mp3)], check=True)
            peak = normalise(mp3, wav); made += 1
            meta.write_text(json.dumps({"sig": sig, "spec": spec, "delivered_peak_db": peak}, indent=1))
            print(f"  {fid}: derived from {src.name} via {f['filter']} -> {wav.name}"); continue
        print(f"  {fid}: generating {spec.get('duration_seconds', 'auto')} s{' loop' if spec['loop'] else ''} ...")
        r = requests.post(f"{API}/v1/sound-generation", params={"output_format": "mp3_44100_128"},
                          headers={**h, "Content-Type": "application/json"}, json=spec, timeout=300)
        if r.status_code != 200:
            print(f"  {fid}: HTTP {r.status_code}: {r.text[:300]}"); continue
        mp3.write_bytes(r.content); peak = normalise(mp3, wav); made += 1
        meta.write_text(json.dumps({"sig": sig, "spec": spec, "delivered_peak_db": peak}, indent=1))
        d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)], capture_output=True, text=True).stdout.strip()
        print(f"  {fid}: {float(d):.1f} s, peak {peak:.1f} dB -> {wav.name}")
    after, _ = credits(h); print(f"generated {made}; credits used: {before - after:,}  ({after:,} left)")

if __name__ == "__main__":
    main()
