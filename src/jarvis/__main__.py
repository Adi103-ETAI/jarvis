"""python3 -m jarvis -- JARVIS console entry point.

  python3 -m jarvis              interactive console loop
  python3 -m jarvis ask "task"   one-shot ask; prints the final assistant text
"""

import sys

from jarvis.console import JarvisConsole, main


def run(argv):
    if argv and argv[0] == "ask" and len(argv) >= 2:
        jc = JarvisConsole()
        try:
            # foreground lane: ask() waits for the turn and returns the reply
            # (or the pause notice if a confirmation code is needed mid-turn)
            out = jc.ask(" ".join(argv[1:]), lane="fg")
            print("--- assistant ---")
            print(out if out else "(no text captured; see journal)")
        finally:
            jc.close()
        return 0
    return main()


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
