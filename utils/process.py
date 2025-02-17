#!/usr/bin/env python
# Copyright (C) 2010-2015 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.
from __future__ import absolute_import
import os
import gc
import sys
import time
import json
import logging
import argparse
import signal
import multiprocessing
import platform
import resource

if sys.version_info[:2] < (3, 6):
    sys.exit("You are running an incompatible version of Python, please use >= 3.6")

try:
    import pebble
except ImportError:
    sys.exit("Missed dependency: pip3 install Pebble")

log = logging.getLogger()

sys.path.append(os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))
from lib.cuckoo.common.colors import red
from lib.cuckoo.common.config import Config
from lib.cuckoo.common.constants import CUCKOO_ROOT
from lib.cuckoo.core.database import Database, Task, TASK_REPORTED, TASK_COMPLETED
from lib.cuckoo.core.database import TASK_FAILED_PROCESSING
from lib.cuckoo.core.plugins import GetFeeds, RunProcessing, RunSignatures
from lib.cuckoo.core.plugins import RunReporting
from lib.cuckoo.core.startup import init_modules, init_yara, ConsoleHandler, check_linux_dist
from concurrent.futures import TimeoutError

cfg = Config()
repconf = Config("reporting")
if repconf.mongodb.enabled:
    from bson.objectid import ObjectId
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure

if repconf.elasticsearchdb.enabled and not repconf.elasticsearchdb.searchonly:
    from elasticsearch import Elasticsearch

    baseidx = repconf.elasticsearchdb.index
    fullidx = baseidx + "-*"
    es = Elasticsearch(hosts=[{"host": repconf.elasticsearchdb.host, "port": repconf.elasticsearchdb.port,}], timeout=60)

check_linux_dist()

pending_future_map = {}
pending_task_id_map = {}

# https://stackoverflow.com/questions/41105733/limit-ram-usage-to-python-program
def memory_limit(percentage: float = 0.8):
    if platform.system() != "Linux":
        print('Only works on linux!')
        return
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (get_memory() * 1024 * percentage, hard))

def get_memory():
    with open('/proc/meminfo', 'r') as mem:
        free_memory = 0
        for i in mem:
            sline = i.split()
            if str(sline[0]) == 'MemAvailable:':
                free_memory = int(sline[1])
                break
    return free_memory

def process(target=None, copy_path=None, task=None, report=False, auto=False, capeproc=False, memory_debugging=False):
    # This is the results container. It's what will be used by all the
    # reporting modules to make it consumable by humans and machines.
    # It will contain all the results generated by every processing
    # module available. Its structure can be observed through the JSON
    # dump in the analysis' reports folder. (If jsondump is enabled.)
    task_dict = task.to_dict() or {}
    task_id = task_dict.get("id") or 0
    results = {"statistics": {"processing": [], "signatures": [], "reporting": []}}
    if memory_debugging:
        gc.collect()
        log.info("[%s] (1) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))
    if memory_debugging:
        gc.collect()
        log.info("[%s] (2) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))
    RunProcessing(task=task_dict, results=results).run()
    if memory_debugging:
        gc.collect()
        log.info("[%s] (3) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))

    RunSignatures(task=task_dict, results=results).run()
    if memory_debugging:
        gc.collect()
        log.info("[%s] (4) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))

    if report:
        if repconf.mongodb.enabled:
            host = repconf.mongodb.host
            port = repconf.mongodb.port
            db = repconf.mongodb.db
            conn = MongoClient(
                host, port=port, username=repconf.mongodb.get("username", None), password=repconf.mongodb.get("password", None), authSource=db
            )
            mdata = conn[db]
            analyses = mdata.analysis.find({"info.id": int(task_id)})
            if analyses.count() > 0:
                log.debug("Deleting analysis data for Task %s" % task_id)
                for analysis in analyses:
                    for process in analysis["behavior"].get("processes", []):
                        for call in process["calls"]:
                            mdata.calls.remove({"_id": ObjectId(call)})
                    mdata.analysis.remove({"_id": ObjectId(analysis["_id"])})
            conn.close()
            log.debug("Deleted previous MongoDB data for Task %s" % task_id)

        if repconf.elasticsearchdb.enabled and not repconf.elasticsearchdb.searchonly:
            analyses = es.search(index=fullidx, doc_type="analysis", q='info.id: "%s"' % task_id)["hits"]["hits"]
            if analyses:
                for analysis in analyses:
                    esidx = analysis["_index"]
                    esid = analysis["_id"]
                    # Check if behavior exists
                    if analysis["_source"]["behavior"]:
                        for process in analysis["_source"]["behavior"]["processes"]:
                            for call in process["calls"]:
                                es.delete(
                                    index=esidx, doc_type="calls", id=call,
                                )
                    # Delete the analysis results
                    es.delete(
                        index=esidx, doc_type="analysis", id=esid,
                    )
        if auto or capeproc:
            reprocess = False
        else:
            reprocess = report

        RunReporting(task=task.to_dict(), results=results, reprocess=reprocess).run()
        Database().set_status(task_id, TASK_REPORTED)

        if auto:
            if cfg.cuckoo.delete_original and os.path.exists(target):
                os.unlink(target)

            if cfg.cuckoo.delete_bin_copy and os.path.exists(copy_path):
                os.unlink(copy_path)

    if memory_debugging:
        gc.collect()
        log.info("[%s] (5) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))
        for i, obj in enumerate(gc.garbage):
            log.info("[%s] (garbage) GC object #%d: type=%s", task_id, i, type(obj).__name__)


def init_worker():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def init_logging(auto=False, tid=0, debug=False):
    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    ch = ConsoleHandler()
    ch.setFormatter(formatter)
    log.addHandler(ch)
    try:
        if not os.path.exists(os.path.join(CUCKOO_ROOT, "log")):
            os.makedirs(os.path.join(CUCKOO_ROOT, "log"))
        if auto:
            if cfg.logging.enabled:
                days = cfg.logging.backup_count or 7
                fh = logging.handlers.TimedRotatingFileHandler(
                    os.path.join(CUCKOO_ROOT, "log", "process.log"), when="midnight", backupCount=int(days)
                )
            else:
                fh = logging.handlers.WatchedFileHandler(os.path.join(CUCKOO_ROOT, "log", "process.log"))
        else:
            fh = logging.handlers.WatchedFileHandler(os.path.join(CUCKOO_ROOT, "log", "process-%s.log" % tid))

    except PermissionError:
        sys.exit("Probably executed with wrong user, PermissionError to create/access log")

    fh.setFormatter(formatter)
    log.addHandler(fh)

    if debug:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)

    logging.getLogger("urllib3").setLevel(logging.WARNING)


def processing_finished(future):
    task_id = pending_future_map.get(future)
    try:
        result = future.result()
        log.info("Task #%d: reports generation completed", task_id)
    except TimeoutError as error:
        log.error("Processing Timeout %s", error)
        Database().set_status(task_id, TASK_FAILED_PROCESSING)
    except pebble.ProcessExpired as error:
        log.error("Exception when processing task %s: %s, Exitcode: %d", task_id, error)
        Database().set_status(task_id, TASK_FAILED_PROCESSING)
    except Exception as error:
        log.error("Exception when processing task %s: %s %s", task_id, error)
        Database().set_status(task_id, TASK_FAILED_PROCESSING)

    del pending_future_map[future]
    del pending_task_id_map[task_id]

def autoprocess(parallel=1, failed_processing=False, maxtasksperchild=7, memory_debugging=False, processing_timeout=300):
    maxcount = cfg.cuckoo.max_analysis_count
    count = 0
    db = Database()
    # pool = multiprocessing.Pool(parallel, init_worker)
    pool = pebble.ProcessPool(max_workers=parallel, max_tasks=maxtasksperchild, initializer=init_worker)
    try:
        memory_limit()
        log.info("Processing analysis data")
        # CAUTION - big ugly loop ahead.
        while count < maxcount or not maxcount:
            # If still full, don't add more (necessary despite pool).
            if len(pending_task_id_map) >= parallel:
                time.sleep(5)
                continue
            if failed_processing:
                tasks = db.list_tasks(status=TASK_FAILED_PROCESSING, limit=parallel, order_by=Task.completed_on.asc())
            else:
                tasks = db.list_tasks(status=TASK_COMPLETED, limit=parallel, order_by=Task.completed_on.asc())
            added = False
            # For loop to add only one, nice. (reason is that we shouldn't overshoot maxcount)
            for task in tasks:
                # Not-so-efficient lock.
                if pending_task_id_map.get(task.id):
                    continue
                log.info("Processing analysis data for Task #%d", task.id)
                if task.category == "file":
                    sample = db.view_sample(task.sample_id)
                    copy_path = os.path.join(CUCKOO_ROOT, "storage", "binaries", sample.sha256)
                else:
                    copy_path = None
                args = task.target, copy_path
                kwargs = dict(report=True, auto=True, task=task, memory_debugging=memory_debugging)
                if memory_debugging:
                    gc.collect()
                    log.info("[%d] (before) GC object counts: %d, %d", task.id, len(gc.get_objects()), len(gc.garbage))
                # result = pool.apply_async(process, args, kwargs)
                future = pool.schedule(process, args, kwargs, timeout=processing_timeout)
                pending_future_map[future] = task.id
                pending_task_id_map[task.id] = future
                future.add_done_callback(processing_finished)
                if memory_debugging:
                    gc.collect()
                    log.info("[%d] (after) GC object counts: %d, %d", task.id, len(gc.get_objects()), len(gc.garbage))
                count += 1
                added = True
                break
            if not added:
                # don't hog cpu
                time.sleep(5)
    except KeyboardInterrupt:
        # ToDo verify in finally
        # pool.terminate()
        raise
    except MemoryError:
        mem = get_memory() / 1024 /1024
        print('Remain: %.2f GB' % mem)
        sys.stderr.write('\n\nERROR: Memory Exception\n')
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
    finally:
        pool.close()
        pool.join()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("id", type=str, help="ID of the analysis to process (auto for continuous processing of unprocessed tasks).")
    parser.add_argument("-c", "--caperesubmit", help="Allow CAPE resubmit processing.", action="store_true", required=False)
    parser.add_argument("-d", "--debug", help="Display debug messages", action="store_true", required=False)
    parser.add_argument("-r", "--report", help="Re-generate report", action="store_true", required=False)
    parser.add_argument("-s", "--signatures", help="Re-execute signatures on the report", action="store_true", required=False)
    parser.add_argument("-p", "--parallel", help="Number of parallel threads to use (auto mode only).", type=int, required=False, default=1)
    parser.add_argument("-fp", "--failed-processing", help="reprocess failed processing", action="store_true", required=False, default=False)
    parser.add_argument("-mc", "--maxtasksperchild", help="Max children tasks per worker", action="store", type=int, required=False, default=7)
    parser.add_argument(
        "-md", "--memory-debugging", help="Enable logging garbage collection related info", action="store_true", required=False, default=False
    )
    parser.add_argument(
        "-pt",
        "--processing-timeout",
        help="Max amount of time spent in processing before we fail a task",
        action="store",
        type=int,
        required=False,
        default=300,
    )
    args = parser.parse_args()

    init_yara()
    init_modules()
    if args.id == "auto":
        init_logging(auto=True, debug=args.debug)
        autoprocess(
            parallel=args.parallel,
            failed_processing=args.failed_processing,
            maxtasksperchild=args.maxtasksperchild,
            memory_debugging=args.memory_debugging,
            processing_timeout=args.processing_timeout,
        )
    else:
        if not os.path.exists(os.path.join(CUCKOO_ROOT, "storage", "analyses", args.id)):
            sys.exit(red("\n[-] Analysis folder doesn't exist anymore\n"))
        init_logging(tid=args.id, debug=args.debug)
        task = Database().view_task(int(args.id))
        if args.signatures:
            report = os.path.join(CUCKOO_ROOT, "storage", "analyses", args.id, "reports", "report.json")
            if not os.path.exists(report):
                sys.exit("File {} doest exist".format(report))

            results = json.load(open(report))
            if results is not None:
                RunSignatures(task=task.to_dict(), results=results).run()
        else:
            process(task=task, report=args.report, capeproc=args.caperesubmit, memory_debugging=args.memory_debugging)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
