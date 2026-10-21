"""Plain Functions Framework functions, the same as you would deploy to Cloud Run functions.

Each one is served at /<name>. A plain Functions Framework function has no schedule of its
own: to run one on a cron, declare it with firebase_functions' @scheduler_fn.on_schedule.
"""

import functions_framework


@functions_framework.http
def echo(request):
    return {
        "method": request.method,
        "path": request.path,
        "args": request.args.to_dict(),
        "body": request.get_data(as_text=True),
    }


@functions_framework.http
def ping(request):
    return "pong"
