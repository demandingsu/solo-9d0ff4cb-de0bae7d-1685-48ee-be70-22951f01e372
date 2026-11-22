#!/bin/sh
set -e
python -c "from app.db import get_db, init_db; c=get_db(); init_db(c); c.close()"
exec python -m flask --app app.routes run --host 0.0.0.0 --port 8000
