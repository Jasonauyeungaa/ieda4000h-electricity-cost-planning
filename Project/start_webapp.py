"""Open this copy of the workbench, starting a local server when needed."""
import errno
import json
import urllib.request
import webbrowser

from study.server import DEFAULT_PORT, WORKBENCH_ID, core, create_server, serve


def url_for(port):
    return f'http://127.0.0.1:{port}/'


def workbench_is_running(url):
    try:
        with urllib.request.urlopen(url + 'api/health', timeout=1) as response:
            health = json.load(response)
        return health == {'workbench': WORKBENCH_ID, 'solver': core.model.SOLVER}
    except (OSError, ValueError):
        return False


def main():
    default_url = url_for(DEFAULT_PORT)
    if workbench_is_running(default_url):
        webbrowser.open(default_url)
        return
    try:
        server = create_server()
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        if workbench_is_running(default_url):
            webbrowser.open(default_url)
            return
        server = create_server(0)
    webbrowser.open(url_for(server.server_port))
    serve(server)


if __name__ == '__main__':
    main()
