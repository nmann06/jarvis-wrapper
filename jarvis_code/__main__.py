"""Open the Jarvis window in this folder.

    jarvis                    a new conversation
    jarvis -c                 continue the last conversation here
    jarvis -r [id]            resume one (without an id, pick from a list)
    jarvis --model sonnet     start on another model (also --effort)
    jarvis --mode default     start in another permission mode (auto unless told otherwise)
    jarvis -p "fix the tests" start with a prompt
    jarvis --colour grey      this window in another colour (/colour changes it for good)
    jarvis --compact          a one-line J.A.R.V.I.S. on top instead of the big letters
    jarvis-grey               both: grey, without the big letters
"""

import argparse
import os
import sys

MODES = ["default", "acceptEdits", "plan", "auto", "bypassPermissions"]
COLOURS = ["blue", "cyan", "teal", "green", "yellow", "orange", "red", "pink", "purple", "grey"]  # as colour.py


def parse(argv):
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis: Claude Code, in a window of its own.")
    parser.add_argument("-c", "--continue", dest="continue_last", action="store_true",
                        help="continue the most recent conversation in this folder")
    parser.add_argument("-r", "--resume", nargs="?", const="", default=None, metavar="ID",
                        help="resume a conversation (pick one if no id is given)")
    parser.add_argument("-p", "--prompt", help="send this as the first message")
    parser.add_argument("--model", help="model to start with (opus, sonnet, haiku, fable, or a full name)")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max", "auto"])
    parser.add_argument("--mode", "--permission-mode", dest="mode", choices=MODES,
                        default=os.environ.get("JARVIS_MODE", "auto"),
                        help="permission mode to start in (default: auto; shift+tab switches later)")
    parser.add_argument("--plain", action="store_true", help="answer as plain Claude Code, without the Jarvis persona")
    parser.add_argument("--thinking", choices=["summarized", "omitted"],
                        default=os.environ.get("JARVIS_THINKING", "summarized"),
                        help="show summaries of Claude's thinking (default), or not")
    parser.add_argument("--colour", "--color", dest="colour", metavar="NAME",
                        help="this window's colour: " + ", ".join(COLOURS) + " (default: /colour's choice)")
    parser.add_argument("--compact", action="store_true",
                        help="a one-line J.A.R.V.I.S. on top instead of the big letters")
    parser.add_argument("--log", metavar="FILE", help="write everything said with Claude Code to FILE, each line timed")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    from .app import JarvisApp, Options, console_title  # after parsing, so --help is instant
    from . import colour, theme, watchdog

    if args.colour and colour.name_of(args.colour) is None:
        print(f"jarvis: no colour {args.colour!r} (there's {', '.join(colour.COLOURS)})", file=sys.stderr)
        return 2
    theme.BIG_LETTERS = not args.compact

    watchdog.install()  # a terminal that stops reading no longer freezes the window

    options = Options(cwd=os.getcwd(), model=args.model, effort=args.effort,
                      permission_mode=args.mode, resume=args.resume, continue_last=args.continue_last,
                      prompt=args.prompt, persona=not args.plain, thinking_display=args.thinking,
                      log_path=args.log, colour=args.colour)
    tty = sys.stdout.isatty()
    old_title = console_title() if tty else None
    if old_title is not None:
        console_title("Jarvis")  # Windows: escape codes may not be on yet, and it restores the same way
    elif tty:
        sys.stdout.write("\033[22;0t\033]0;Jarvis\007")  # keep the old title, show Jarvis
        sys.stdout.flush()
    try:
        session = JarvisApp(options).run()
    finally:
        if old_title is not None:
            console_title(old_title)
        elif tty:
            sys.stdout.write("\033[23;0t")
            sys.stdout.flush()
    if session and tty:
        print(f"\033[2mResume this conversation with: jarvis -r {session}\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
