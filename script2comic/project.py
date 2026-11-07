"""Project directory and secrets for script2comic.

Everything a script produces or reads lives in ONE project directory:

    my-film/
      .env                 ELEVENLABS_API_KEY=...  ATLAS_API_KEY=...   (or set them in the environment)
      script.fountain      the screenplay (any single *.fountain file is found automatically)
      cast.json            voice per character                      (voices)
      directions.json      per-line performance notes, optional     (voices)
      refs.yaml            character descriptions + reference plates (board)
      refs/                the plate images named in refs.yaml
      style.yaml           prompt style blocks, optional            (board)
      storyboards/*.yaml   one board per act / cut                  (board)
      music.yaml sfx.yaml  themes, effects and cues, optional       (music, sfx, board assemble)
      cache/               voice clips, generated frames, rendered clips (safe to delete, costs to regenerate)
      out/                 audio cuts + timelines, finished videos
      ledger-voices.jsonl  every ElevenLabs request and what it charged
      ledger-images.jsonl  every image generated and its price

The project is the current directory unless `--project DIR` is on the command line or
S2C_PROJECT is set. `--project` is taken out of argv here so every tool sees the same thing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _take_project_arg() -> Path:
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--project" and i + 1 < len(argv):
            p = argv[i + 1]
            del argv[i:i + 2]
            return Path(p).expanduser().resolve()
        if a.startswith("--project="):
            del argv[i]
            return Path(a.split("=", 1)[1]).expanduser().resolve()
    env = os.environ.get("S2C_PROJECT")
    return Path(env).expanduser().resolve() if env else Path.cwd().resolve()


PROJECT = _take_project_arg()
CACHE = PROJECT / "cache"
OUT = PROJECT / "out"


def load_env() -> None:
    """KEY=value lines from <project>/.env go into the environment (existing values win)."""
    p = PROJECT / ".env"
    if not p.exists():
        return
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"{name} is not set. Put it in {PROJECT / '.env'} or the environment.")
    return val


def script_path(explicit: str | Path | None = None) -> Path:
    """The screenplay: --script, else <project>/script.fountain, else the only *.fountain there."""
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = (PROJECT / p).resolve()
        return p
    p = PROJECT / "script.fountain"
    if p.exists():
        return p
    found = sorted(PROJECT.glob("*.fountain"))
    if len(found) == 1:
        return found[0]
    if not found:
        sys.exit(f"no screenplay: put script.fountain in {PROJECT} or pass --script")
    sys.exit(f"several .fountain files in {PROJECT}; pass --script")


def add_project_arg(parser) -> None:
    """For --help only: the value itself is consumed before argparse runs."""
    parser.add_argument("--project", metavar="DIR", help="project directory (default: current directory, or $S2C_PROJECT)")
