"""A Firebase Functions project, exactly as `firebase deploy` would take it.

miniFaaS reads the same decorators: `hello` is served at /hello, and `heartbeat` runs on its
own schedule. Neither knows miniFaaS exists.
"""

import os
from datetime import datetime

from firebase_functions import https_fn, scheduler_fn
from firebase_functions.params import SecretParam

GREETING = SecretParam("GREETING")


@https_fn.on_request(secrets=[GREETING])
def hello(req: https_fn.Request) -> https_fn.Response:
    name = req.args.get("name", "world")
    greeting = os.environ.get("GREETING", "Hello")
    return https_fn.Response(f"{greeting}, {name}!")


@scheduler_fn.on_schedule(schedule="every 5 minutes", timezone="America/New_York")
def heartbeat(event: scheduler_fn.ScheduledEvent) -> None:
    print(f"Heartbeat for {event.schedule_time:%H:%M}, run at {datetime.now():%H:%M:%S}")
