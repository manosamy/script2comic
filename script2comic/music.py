"""Generate the background-music themes listed in <project>/music.yaml with ElevenLabs Music
(POST /v1/music), once each: a theme is re-generated only when its prompt/length/model change.
Output: <project>/music/<theme>.mp3 (as delivered) and <theme>.wav (normalised to -20 LUFS, the
file the mixer loops). Credits are read from the subscription before and after.
usage: s2c music [--project DIR] [theme ...] [--force]"""
import hashlib, json, subprocess, sys
from pathlib import Path
import requests, yaml
if __package__:
    from . import project
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from script2comic import project
API = "https://api.elevenlabs.io"
CAT = project.PROJECT / "music.yaml"; OUT = project.PROJECT / "music"
TARGET_LUFS = -20

def credits(h):
    s = requests.get(f"{API}/v1/user/subscription", headers=h, timeout=30).json()
    return s["character_limit"] - s["character_count"], s["tier"]

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]; force = "--force" in sys.argv
    key = project.env("ELEVENLABS_API_KEY", required=True); h = {"xi-api-key": key}
    if not CAT.exists():
        sys.exit(f"no {CAT} (see example/music.yaml)")
    OUT.mkdir(exist_ok=True)
    themes = yaml.safe_load(CAT.read_text(encoding="utf-8"))["themes"]
    before, tier = credits(h); print(f"account: {tier}, {before:,} credits")
    for tid, t in themes.items():
        if args and tid not in args: continue
        spec = {"prompt": " ".join(t["prompt"].split()), "music_length_ms": int(t["length_s"] * 1000),
                "model_id": t.get("model", "music_v2"), "force_instrumental": True}
        sig = hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]
        mp3, wav, meta = OUT / f"{tid}.mp3", OUT / f"{tid}.wav", OUT / f"{tid}.json"
        if wav.exists() and meta.exists() and json.loads(meta.read_text())["sig"] == sig and not force:
            print(f"  {tid}: cached"); continue
        print(f"  {tid}: composing {t['length_s']} s with {spec['model_id']} ...")
        r = requests.post(f"{API}/v1/music", params={"output_format": "mp3_44100_192"}, headers={**h, "Content-Type": "application/json"},
                          json=spec, timeout=600)
        if r.status_code != 200:
            sys.exit(f"  {tid}: HTTP {r.status_code}: {r.text[:400]}")
        mp3.write_bytes(r.content)
        # measure, then normalise to TARGET_LUFS so every theme sits at the same level under the voice
        m = subprocess.run(["ffmpeg", "-i", str(mp3), "-af", "loudnorm=print_format=json", "-f", "null", "-"], capture_output=True, text=True).stderr
        lufs = float(json.loads(m[m.rfind("{"): m.rfind("}") + 1])["input_i"])
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-af", f"volume={TARGET_LUFS - lufs:.2f}dB",
                        "-ar", "48000", "-ac", "2", str(wav)], check=True)
        meta.write_text(json.dumps({"sig": sig, "spec": spec, "delivered_lufs": lufs}, indent=1))
        print(f"  {tid}: {mp3.stat().st_size / 1e6:.1f} MB, delivered at {lufs:.1f} LUFS -> {wav.name} @ {TARGET_LUFS} LUFS")
    after, _ = credits(h); print(f"credits used: {before - after:,}  ({after:,} left)")

if __name__ == "__main__":
    main()
