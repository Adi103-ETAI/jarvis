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
            print(jc.ask(" ".join(argv[1:])))
            final = jc.wait_turn(timeout=240)
            print("--- assistant ---")
            print(final if final else "(no text captured; see journal)")
        finally:
            jc.close()
        return 0
    return main()


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
