import atexit
from argparse import ArgumentParser
from hashlib import md5

from flask import Flask, request, Response
from flask_compress import Compress
from flask_cors import CORS
from semantic_version import Version
from werkzeug.exceptions import BadRequest

import database
from apierrors.base import BaseError
from bll.statistics.stats_reporter import StatisticsReporter
from config import config, info
from elastic.initialize import init_es_data, check_elastic_empty, ElasticConnectionError
from mongo.initialize import (
    init_mongo_data,
    pre_populate_data,
    check_mongo_empty,
    get_last_server_version,
)
from service_repo import ServiceRepo, APICall
from service_repo.auth import AuthType
from service_repo.errors import PathParsingError
from sync import distributed_lock
from timing_context import TimingContext
from updates import check_updates_thread
from utilities import json
from utilities.threads_manager import ThreadsManager

app = Flask(__name__, static_url_path="/static")
CORS(app, **config.get("apiserver.cors"))
Compress(app)

log = config.logger(__file__)

log.info("################ API Server initializing #####################")

app.config["SECRET_KEY"] = config.get("secure.http.session_secret.apiserver")
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = config.get("apiserver.pretty_json")

database.initialize()

# build a key that uniquely identifies specific mongo instance
hosts_string = ";".join(sorted(database.get_hosts()))
key = "db_init_" + md5(hosts_string.encode()).hexdigest()
with distributed_lock(key, timeout=config.get("apiserver.db_init_timout", 120)):
    upgrade_monitoring = config.get(
        "apiserver.elastic.upgrade_monitoring.v16_migration_verification", True
    )
    try:
        empty_es = check_elastic_empty()
    except ElasticConnectionError as err:
        if not upgrade_monitoring:
            raise
        log.error(err)
        info.es_connection_error = True

    empty_db = check_mongo_empty()
    if (
        upgrade_monitoring
        and not empty_db
        and (info.es_connection_error or empty_es)
        and get_last_server_version() < Version("0.16.0")
    ):
        log.info(f"ES database seems not migrated")
        info.missed_es_upgrade = True

    if info.es_connection_error and not info.missed_es_upgrade:
        raise Exception(
            "Error starting server: failed connecting to ElasticSearch service"
        )

    if not info.missed_es_upgrade:
        init_es_data()
        init_mongo_data()

if (
    not info.missed_es_upgrade
    and empty_db
    and config.get("apiserver.pre_populate.enabled", False)
):
    pre_populate_data()


ServiceRepo.load("services")
log.info(f"Exposed Services: {' '.join(ServiceRepo.endpoint_names())}")


check_updates_thread.start()
StatisticsReporter.start()


def graceful_shutdown():
    ThreadsManager.terminating = True


atexit.register(graceful_shutdown)


@app.before_first_request
def before_app_first_request():
    pass


@app.before_request
def before_request():
    if request.method == "OPTIONS":
        return "", 200
    if "/static/" in request.path:
        return

    try:
        call = create_api_call(request)
        content, content_type = ServiceRepo.handle_call(call)
        headers = {}
        if call.result.filename:
            headers[
                "Content-Disposition"
            ] = f"attachment; filename={call.result.filename}"

        if call.result.headers:
            headers.update(call.result.headers)

        response = Response(
            content, mimetype=content_type, status=call.result.code, headers=headers
        )

        if call.result.cookies:
            for key, value in call.result.cookies.items():
                if value is None:
                    response.set_cookie(key, "", expires=0)
                else:
                    response.set_cookie(
                        key, value, **config.get("apiserver.auth.cookies")
                    )

        return response
    except Exception as ex:
        log.exception(f"Failed processing request {request.url}: {ex}")
        return f"Failed processing request {request.url}", 500


def update_call_data(call, req):
    """ Use request payload/form to fill call data or batched data """
    if req.content_type == "application/json-lines":
        items = []
        for i, line in enumerate(req.data.splitlines()):
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise BadRequest(
                        f"json lines must contain objects, found: {type(event).__name__}"
                    )
                items.append(event)
            except ValueError as e:
                msg = f"{e} in batch item #{i}"
                req.on_json_loading_failed(msg)
        call.batched_data = items
    else:
        json_body = req.get_json(force=True, silent=False) if req.data else None
        # merge form and args
        form = req.form.copy()
        form.update(req.args)
        form = form.to_dict()
        # convert string numbers to floats
        for key in form:
            if form[key].replace(".", "", 1).isdigit():
                if "." in form[key]:
                    form[key] = float(form[key])
                else:
                    form[key] = int(form[key])
            elif form[key].lower() == "true":
                form[key] = True
            elif form[key].lower() == "false":
                form[key] = False
        call.data = json_body or form or {}


def _call_or_empty_with_error(call, req, msg, code=500, subcode=0):
    call = call or APICall(
        "", remote_addr=req.remote_addr, headers=dict(req.headers), files=req.files
    )
    call.set_error_result(msg=msg, code=code, subcode=subcode)
    return call


def create_api_call(req):
    call = None
    try:
        # Parse the request path
        endpoint_version, endpoint_name = ServiceRepo.parse_endpoint_path(req.path)

        # Resolve authorization: if cookies contain an authorization token, use it as a starting point.
        # in any case, request headers always take precedence.
        auth_cookie = req.cookies.get(
            config.get("apiserver.auth.session_auth_cookie_name")
        )
        headers = (
            {}
            if not auth_cookie
            else {"Authorization": f"{AuthType.bearer_token} {auth_cookie}"}
        )
        headers.update(
            list(req.headers.items())
        )  # add (possibly override with) the headers

        # Construct call instance
        call = APICall(
            endpoint_name=endpoint_name,
            remote_addr=req.remote_addr,
            endpoint_version=endpoint_version,
            headers=headers,
            files=req.files,
        )

        # Update call data from request
        with TimingContext("preprocess", "update_call_data"):
            update_call_data(call, req)

    except PathParsingError as ex:
        call = _call_or_empty_with_error(call, req, ex.args[0], 400)
        call.log_api = False
    except BadRequest as ex:
        call = _call_or_empty_with_error(call, req, ex.description, 400)
    except BaseError as ex:
        call = _call_or_empty_with_error(call, req, ex.msg, ex.code, ex.subcode)
    except Exception as ex:
        log.exception("Error creating call")
        call = _call_or_empty_with_error(
            call, req, ex.args[0] if ex.args else type(ex).__name__, 500
        )

    return call


# =================== MAIN =======================
if __name__ == "__main__":
    p = ArgumentParser(description=__doc__)
    p.add_argument(
        "--port", "-p", type=int, default=config.get("apiserver.listen.port")
    )
    p.add_argument("--ip", "-i", type=str, default=config.get("apiserver.listen.ip"))
    p.add_argument(
        "--debug", action="store_true", default=config.get("apiserver.debug")
    )
    p.add_argument(
        "--watch", action="store_true", default=config.get("apiserver.watch")
    )
    args = p.parse_args()

    # logging.info("Starting API Server at %s:%s and env '%s'" % (args.ip, args.port, config.env))

    app.run(
        debug=args.debug,
        host=args.ip,
        port=args.port,
        threaded=True,
        use_reloader=args.watch,
    )
