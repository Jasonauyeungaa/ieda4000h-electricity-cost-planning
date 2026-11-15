"""Open the workbench, starting its local server when needed."""
import json
import threading
import time
import urllib.request
import webbrowser

from study.server import main

URL = 'http://127.0.0.1:8156/'


def workbench_is_running():
    try:
        with urllib.request.urlopen(URL + 'api/reference', timeout=1) as response:
            reference = json.load(response)
        return reference.get('recorded') is True and 'run' in reference
    except (OSError, ValueError):
        return False


def open_when_ready():
    for _ in range(30):
        if workbench_is_running():
            webbrowser.open(URL)
            return
        time.sleep(0.3)


if __name__ == '__main__':
    if workbench_is_running():
        webbrowser.open(URL)
    else:
        threading.Thread(target=open_when_ready, daemon=True).start()
        main()
