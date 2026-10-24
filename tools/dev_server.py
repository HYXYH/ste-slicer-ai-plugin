"""Run the real HTTP/session/upload stack without STE Slicer (Python 3.5+)."""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin"))
from AIAssistant.session import SessionStore
from AIAssistant.slicer_bridge import DemoBridge
from AIAssistant.server import LocalServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=".local-assistant-data")
    args = parser.parse_args()
    store = SessionStore(os.path.abspath(args.data_dir))
    server = LocalServer(store, DemoBridge(store))
    print(server.browser_url(), flush=True)
    print("Development mode: no scene import. Stop with Ctrl+C.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
