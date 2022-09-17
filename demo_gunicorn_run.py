import sys

from gunicorn.app.wsgiapp import run

sys.argv.extend(
    [
        "--workers=2",
        "--worker-class=sync",
        "--bind=127.0.0.1:9000",
        "myapp:app",
    ]
)

run()
