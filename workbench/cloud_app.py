"""Single-owner deployment entrypoint. Requires external TLS and a durable volume."""
import os
from pathlib import Path
from .cloud_auth import CloudAuth, Config
from .service import Service
from .server import make_server


def main():
    # Missing authentication configuration aborts startup before opening a socket.
    auth = CloudAuth(Config.from_env(os.environ))
    state = os.environ.get('NEUROMORPH_STATE_DIR')
    if not state or not Path(state).is_absolute():
        raise SystemExit('NEUROMORPH_STATE_DIR must identify an absolute persistent volume')
    service = Service(db_path=Path(state)/'workbench.sqlite3')
    server = make_server(service, host='0.0.0.0', port=int(os.environ.get('PORT','8080')), cloud_auth=auth)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
