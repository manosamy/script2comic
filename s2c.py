#!/usr/bin/env python3
"""script2comic -- screenplay to motion comic.

    python s2c.py voices    [--project DIR] [--dry-run] ...     render the script with ElevenLabs -> out/<name>-narrated.mp3 + timeline
    python s2c.py board     [--project DIR] gen|sheet|edit|fix|assemble|status|prune|merge <board> ...
    python s2c.py music     [--project DIR] [theme ...]          generate the themes in music.yaml
    python s2c.py sfx       [--project DIR] [effect ...]         generate the effects in sfx.yaml
    python s2c.py voiceover [--project DIR] <job.json>           a standalone voice-over (pitch narration)
    python s2c.py pitch     [--project DIR] <cues.yaml>          a pitch cut from board frames + audio cues

Run any tool with -h for its options. --project may also be the S2C_PROJECT environment variable.
"""
import runpy
import sys
from pathlib import Path

TOOLS = {"voices", "board", "music", "sfx", "voiceover", "pitch"}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help") or sys.argv[1] not in TOOLS:
        print(__doc__)
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
    tool = sys.argv.pop(1)
    sys.argv[0] = f"s2c {tool}"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    runpy.run_module(f"script2comic.{tool}", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
