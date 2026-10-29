import os
from .api import server
from .db import Database
from .service import Service
from .limits import Limits

if __name__ == '__main__':
    app = server(Service(Database(os.getenv('GEO_DB', 'data/geo.sqlite3')), Limits.from_env()),
                 os.getenv('GEO_API_TOKEN', ''), port=int(os.getenv('GEO_PORT', '8000')))
    print(f'Xing GEO listening on http://127.0.0.1:{app.server_port}', flush=True)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.server_close()
