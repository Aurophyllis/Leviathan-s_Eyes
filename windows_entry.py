"""Entry point for the Windows x64 portable bundle."""

import argparse
import os
from pathlib import Path
import sys


def run_mcp():
    from mcp_connector import main

    original_in, original_out = sys.stdin, sys.stdout
    # The SDK's text wrappers take ownership of their buffers. Keep the
    # bootloader's original handles available for its shutdown flush.
    with (os.fdopen(os.dup(original_in.fileno()), "r", encoding="utf-8") as input_stream,
          os.fdopen(os.dup(original_out.fileno()), "w", encoding="utf-8") as output_stream):
        try:
            sys.stdin, sys.stdout = input_stream, output_stream
            main()
        finally:
            sys.stdin, sys.stdout = original_in, original_out


def main():
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    bundled_tools = root / "ffmpeg" / "bin"
    if bundled_tools.is_dir():
        os.environ["PATH"] = str(bundled_tools) + os.pathsep + os.environ.get("PATH", "")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("desktop", "service", "mcp"), default="desktop")
    remaining = sys.argv[1:]
    if remaining and remaining[0] in ("-h", "--help"):
        parser.parse_args(remaining)
    command = remaining.pop(0) if remaining and remaining[0] in ("desktop", "service", "mcp") else "desktop"
    sys.argv[1:] = remaining
    if command == "service":
        from service import main as run
    elif command == "mcp":
        run = run_mcp
    else:
        from desktop import main as run

        if not any(item == "--output" or item.startswith("--output=") for item in remaining):
            data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LeviathanEyes" / "observations"
            sys.argv.extend(["--output", str(data)])
    run()


if __name__ == "__main__":
    main()
