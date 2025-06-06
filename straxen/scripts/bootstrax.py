"""
Bootstrax: XENONnT online processing manager
=============================================
How to use
----------------
    <activate conda environment>
    bootstrax --production

    or with the preferred settings:
    bootstrax --production --infer_mode --delete_live --undying

----------------

For more info, see the documentation:
https://straxen.readthedocs.io/en/latest/bootstrax.html
"""

__version__ = "2.0.1"

import os
import os.path as osp
import typing as ty
import argparse
import typing
from datetime import datetime, timedelta, timezone
import logging
import multiprocessing
import npshmex
import signal
import socket
import shutil
import time
import traceback
import numpy as np
import pymongo
from psutil import pid_exists, disk_usage, Process
import pytz
import strax
import straxen
import threading
import pandas as pd
import daqnt
import fnmatch
from glob import glob
from straxen import daq_core
from straxen.daq_core import now


# Patch for targeted (uncompressed) chunk size
straxen.Peaklets.chunk_target_size_mb = strax.DEFAULT_CHUNK_SIZE_MB
straxen.nVETOHitlets.chunk_target_size_mb = strax.DEFAULT_CHUNK_SIZE_MB

parser = argparse.ArgumentParser(description="XENONnT online processing manager")
parser.add_argument(
    "--debug", action="store_true", help="Start strax processes with debug logging."
)
parser.add_argument(
    "--profile",
    type=str,
    default="false",
    help="Run strax in profiling mode. Specify target filename as argument.",
)
parser.add_argument(
    "--cores", type=int, default=8, help="Maximum number of workers to use in a strax process."
)
parser.add_argument(
    "--targets",
    nargs="*",
    default="event_info events_nv events_mv online_peak_monitor veto_proximity".split(),
    help="Strax data type name(s) that should be produced with live processing",
)
parser.add_argument(
    "--post_process",
    nargs="*",
    default=["veto_intervals"],
    help=(
        "Target(s) for other sub-detectors. If not produced automatically "
        "when processing tpc data, st.make the requested data later."
    ),
)
parser.add_argument(
    "--fix_target",
    action="store_true",
    help="Don't allow bootstrax to switch to a different target for special runs",
)
parser.add_argument(
    "--fix_resources",
    action="store_true",
    help="Don't let bootstrax change number of cores/max_messages because of failures",
)
parser.add_argument(
    "--infer_mode",
    action="store_true",
    help="Determine best number max-messages and cores for each run "
    "automatically. Overrides --cores and --max_messages",
)
parser.add_argument(
    "--delete_live",
    action="store_true",
    help="Delete live_data after successful processing of the run.",
)
parser.add_argument(
    "--production",
    action="store_true",
    help=(
        "Run bootstrax in production mode. Assuming test mode otherwise to "
        "prevent interactions with the runs-database"
    ),
)
parser.add_argument(
    "--ignore_checks",
    action="store_true",
    help="Do not use! This disables checks on e.g. the timestamps! Should only "
    "be used if some run is very valuable but some checks are failing.",
)
parser.add_argument("--max_messages", type=int, default=10, help="number of max mailbox messages")


actions = parser.add_mutually_exclusive_group()
actions.add_argument(
    "--process", type=int, metavar="NUMBER", help="Process a single run, regardless of its status."
)
actions.add_argument(
    "--fail",
    nargs="+",
    metavar=("NUMBER", "REASON"),
    help="Fail run number, optionally with reason",
)
actions.add_argument(
    "--abandon",
    nargs="+",
    metavar=("NUMBER", "REASON"),
    help="Abandon run number, optionally with reason",
)
actions.add_argument("--undying", action="store_true", help="Except any error and ignore it")
args = parser.parse_args()

##
# Configuration
##

# The folder that can be used for testing bootstrax (i.e. non production
# mode). It will be written to:
test_data_folder = (
    "/data/test_processed/" if os.path.exists("/data/test_processed/") else "./bootstrax/"
)

# Timeouts in seconds
timeouts = {
    # Waiting between escalating SIGTERM -> SIGKILL -> crashing bootstrax
    # when trying to kill another process (usually child strax)
    "signal_escalate": 3,
    # Minimum waiting time to retry a failed run
    # Escalates exponentially on repeated failures: 1x, 5x, 25x, 125x, 125x, 125x, ...
    # Some jitter is applied: actual delays will randomly be 0.5 - 1.5x as long
    "retry_run": 60,
    # Maximum time [s] for strax to complete a processing
    # if exceeded, strax will be killed by bootstrax
    "max_processing_time": 72 * 3600,
    # Max processing time [s] for bootstrax to finish processing after run ended
    "max_processing_time_after_completion": 13 * 3600,
    # Every this many [s] we should have at least one new file written to disk
    "max_no_write_time": 30 * 60,
    # Sleep between checking whether a strax process is alive
    "check_on_strax": 10,
    # Maximum time a run is 'busy' without a further update from
    # its responsible bootstrax. Bootstrax normally updates every
    # check_on_strax seconds, so make sure this is substantially
    # larger than check_on_strax.
    "max_busy_time": 120,
    # Maximum time a run is in the 'considering' state
    # if exceeded, will be labeled as an untracked failure
    "max_considering_time": 60,
    # Minimum time to wait between database cleanup operations
    "cleanup_spacing": 60,
    # Sleep time when there is nothing to do
    "idle_nap": 10,
    # If we don't hear from a bootstrax on another host for this long,
    # remove its entry from the bootstrax status collection
    # Must be much longer than idle_nap and check_on_strax!
    "bootstrax_presumed_dead": 300,
    # Ebs3-5 normally do all the processing. However if all are busy
    # for a longer period of time, the ebs0-2 can also help with
    # processing.
    "eb3-5_max_busy_time": 5 * 60,
    # Bootstrax writes it's state to the daq-database. To have a backlog we store this
    # state using a TTL collection. To prevent too many entries in this backlog, only
    # create new entries if the previous entry is at least this old (in seconds).
    "min_status_interval": 60,
    # Maximum time we can take to can infer the datarate (s).
    "max_data_rate_infer_time": 60,
    # Minimum time we have to be in the run before we can infer the datarate (s).
    "min_data_rate_infer_time": 7.5,
    # Bootstrax can abandon runs for this time based on a tag, after that it
    # should not be on the DAQ any way or can be manually failed using the
    # --abandon option. We can abandon a run only for this many seconds:
    "abandoning_allowed": 3600 * 24 * 1,
}

# The disk that the eb is writing to may fill up at some point. The data should
# be written to datamanager at some point. This may clean up data on the disk,
# hence, we can check if there is sufficient diskspace and if not, wait a while.
# Below are the max number of times and number of seconds bootstrax will wait.
wait_diskspace_max_space_percent = 95
min_disk_space_tb = 1.0  # Terabytes
wait_diskspace_n_max = 60 * 24 * 7  # times
wait_diskspace_dt = 10  # seconds
if timeouts["bootstrax_presumed_dead"] < wait_diskspace_dt:
    raise ValueError("wait_diskspace_dt too large")

# Fields in the run docs that bootstrax uses. Pay attention to the tailing spaces!
bootstrax_projection = (
    "name start end number bootstrax status mode detectors rate "
    "data.host "
    "data.type "
    "data.location "
    "daq_config.processing_threads "
    "daq_config.compressor "
    "daq_config.strax_fragment_payload_bytes "
    "daq_config.strax_chunk_length "
    "daq_config.strax_chunk_overlap".split()
)

# Filename for temporary storage of the exception
# This is used to communicate the exception from the strax child process
# to the bootstrax main process
exception_tempfile = "last_bootstrax_exception.txt"

# The name of the thread that is opened to delete live_data
delete_thread_name = "DeleteThread"

# boostrax state for 'dead' or old entries in the bs_coll
dead_state = "dead_bootstrax"

# The maximum time difference (s) allowed between the timestamps in the data and the
# duration of the run (from the runs metadeta). Fail if the difference is larger than:
max_timetamp_diff = 5

# The maximum number of retries for processing a run. After this many times of retrying
# to process a run, the DAQ-group has to either manually fix this run or manually fail it.
max_n_retry = 10

# Check that there are runs that are waiting to be processed. If there are few,
# an eventbuilder should not process new data.
max_queue_new_runs = 3

# Remove any targets or post processing targets after the run failed
# this many times. If high level data is hitting some edge case, we
# might want to be able to keep the intermediate level data.
# NB: fnmatch so event* applies e.g. to event_basics, peak* to e.g. peaklets
# See https://xe1t-wiki.lngs.infn.it/doku.php?id=cfuselli:bootstrax_fail_targets
remove_target_after_fails = {
    "event*": 2,
    "corrected_areas": 2,
    "energy_estimates": 2,
    "distinct_channels": 2,
    "*pos*": 3,
    "individual_peak*": 3,
    "online_monitor_*v": 3,
    "peak_*": 4,
    "online_peak*": 4,
    "*hit*": 4,
    "veto_*": 4,
    "*pulse*": 4,
    "led_*": 4,
    "ext_timings_nv": 4,
    "merged_s2s*": 5,
    "peak*": 6,
    "lone_*": 6,
    "detector_time_*": 6,
    "*": 7,
}

##
# Initialize globals (e.g. rundb connection)
##
hostname = socket.getfqdn()

versions = straxen.print_versions(
    modules="strax straxen utilix daqnt numpy tensorflow numba".split(),
    include_git=True,
    return_string=True,
)

log_name = "bootstrax_" + hostname + ("" if args.production else "_TESTING")
log = daqnt.get_daq_logger(
    log_name,
    log_name,
    level=logging.DEBUG,
    opening_message=f"I am processing with these software versions: {versions}",
)

# Set the output folder
output_folder = daq_core.pre_folder if args.production else test_data_folder

if not args.production:
    # This means we are in some test mode
    wait_diskspace_max_space_percent = 80
    if not os.path.exists(output_folder):
        log.warning(f"Creating {output_folder}")
        os.mkdir(output_folder)
    log.warning(
        "\n---------------"
        "\nBe aware, bootstrax not running in production mode. Specify with --production."
        f"\nWriting new data to {output_folder}. Not saving this location in the RunDB."
        "\nNot writing to the runs-database."
        "\n---------------"
    )
    time.sleep(5)
else:
    if not args.delete_live:
        log.warning(
            "Production mode is designed to run with '--delete_live'\nplease restart bootstrax"
        )
    if not args.infer_mode:
        log.warning(
            "Better performance is expected in production mode with "
            "'--infer_mode'\nplease restart bootstrax"
        )

if os.access(output_folder, os.W_OK) is not True:
    message = f"No writing access to {output_folder}"
    log.warning(message)
    raise IOError(message)


def new_context(
    cores=args.cores,
    max_messages=args.max_messages,
    timeout=500,
):
    """Create strax context that can access the runs db."""
    # We use exactly the same logic of straxen to access the runs DB;
    # this avoids duplication, and ensures strax can access the runs DB if we can

    context = straxen.contexts.xenonnt_online(
        output_folder=output_folder,
        we_are_the_daq=True,
        allow_multiprocess=cores > 1,
        allow_shm=cores > 1,
        allow_lazy=False,
        max_messages=max_messages,
        timeout=timeout,
        _rucio_path=None,
    )
    if not args.production:
        # Keep the rundb but set it to readonly and local only, delete
        # all other storage frontends except fo the test folder.
        context.storage = [context.storage[0], strax.DataDirectory(output_folder)]
        context.storage[0].readonly = True
        context.storage[0].local_only = True
    return context


st = new_context()

databases = daq_core.DataBases(production=args.production)
run_db = databases.run_db
daq_db = databases.daq_db
run_coll = databases.run_coll
log_coll = databases.log_coll
ag_stat_coll = databases.ag_stat_coll
bs_coll = databases.bs_coll
run_db.command("ping")

# Ping the databases to ensure the mongo connections are working
if not args.undying:
    run_db.command("ping")
    daq_db.command("ping")


def run():
    if args.cores == -1:
        # Use all of the available cores on this machine
        args.cores = multiprocessing.cpu_count()
        log.info(f"Set cores to n_tot, using {args.cores} cores")

    if args.fail:
        args.fail += [""]  # Provide empty reason if none specified
        manual_fail(number=int(args.fail[0]), reason=args.fail[1])

    elif args.abandon:
        number = int(args.abandon[0])
        if len(args.abandon) > 1:
            manual_fail(number=number, reason=args.abandon[1])
        abandon(number=number)

    elif args.process:
        t_start = now()
        number = args.process

        # Check whether the run is already processed
        bootstrax_state = (
            run_coll.find_one({"number": number}, projection={"bootstrax": True})
            .get("bootstrax", {})
            .get("state", "no-state")
        )
        if args.production and bootstrax_state in ["done", "busy", "considering"]:
            message = f"It looks like run {number} is already processed."
            log_warning(message, priority="fatal")
            raise ValueError(message)

        rd = consider_run({"number": number})
        if rd is None:
            message = f"Trying to process single run but no run numbered {number} exists"
            log_warning(message, priority="fatal")
            raise ValueError(message)

        set_state("busy")
        process_run(rd)
        log.info(
            f"bootstrax ({hostname}) finished run {number} in"
            f" {(now() - t_start).total_seconds()} seconds"
        )
        wait_on_delete_thread()

    else:
        # Start processing
        loop()


##
# Main loop
##


def loop():
    """Infinite loop looking for runs to process."""
    # Ensure we're the only bootstrax on this host
    any_other_running = list(bs_coll.find({"host": hostname, "pid": {"$ne": os.getpid()}}))
    for x in any_other_running:
        if pid_exists(x["pid"]) and x["pid"]:
            log.warning(f'Bootstrax already running with PID {x["pid"]}, trying to kill it.')
            kill_process(x["pid"])

    # # Register ourselves
    set_state("starting")
    t_start = now()

    next_cleanup_time = now()
    # keep track of the ith run that we have seen when we are not in production mode
    new_runs_seen, failed_runs_seen = 0, 1
    while True:
        log.info(f"bootstrax running for {(now() - t_start).total_seconds()} seconds")
        # Check resources are still OK, otherwise crash / reboot program
        sufficient_diskspace()
        log.info("Looking for work")
        set_state("busy")
        if eb_can_process():
            # Process new runs
            rd = consider_run({"bootstrax.state": None}, test_counter=new_runs_seen)
            if rd is not None:
                new_runs_seen += 1
                process_run(rd)
                continue
        else:
            # We are on an old eb with not so much to do, perhaps one of
            # the veto systems needs processing?
            rd = consider_run(
                {"detectors": {"$ne": "tpc"}, "bootstrax.state": None}, test_counter=new_runs_seen
            )
            if rd is not None:
                new_runs_seen += 1
                process_run(rd)
                continue

        # There is either no new run or we are an old eventbuilder.
        # Scan DB for runs with unusual problems
        if now() > next_cleanup_time:
            cleanup_db()
            next_cleanup_time = now(plus=timeouts["cleanup_spacing"])

        # Any failed runs to retry?
        # Only try one run, we want to be back for new runs quickly
        rd = consider_run(
            {
                "bootstrax.state": "failed",
                "bootstrax.n_failures": {"$lt": max_n_retry},
                "bootstrax.next_retry": {"$lt": now()},
            },
            test_counter=failed_runs_seen,
        )

        if rd is not None:
            failed_runs_seen += 1
            process_run(rd)
            continue
        # Nothing to do, let's do some cleanup
        if not args.production:
            log.info(
                "We have gone through the rundDB in a readonly mode there are no "
                f"runs left. We looked at {new_runs_seen} new runs and "
                f"{failed_runs_seen} previously failed runs."
            )
            break
        log.info("No work to do, waiting for new runs or retry timers")
        set_state("idle")
        time.sleep(timeouts["idle_nap"])


##
# General helpers
##


def kill_process(pid):
    """Kill process pid."""
    log.warning(f"Kill PID:{pid}")
    if not pid_exists(pid):
        log.warning(f"No PID:{pid}")
        return

    parent = Process(pid)
    for child in parent.children(recursive=True):
        child.kill()
    parent.kill()

    # Just make it extra dead
    os.kill(pid, signal.SIGKILL)

    if pid_exists(pid):
        message = f"Could not kill process {pid}?!"
        log_warning(message, priority="fatal")


def _remove_veto_from_t(
    targets: ty.Union[str, list, tuple],
    remove: ty.Union[str, list, tuple] = ("_mv", "_nv"),
    _flip: bool = False,
) -> ty.Union[str, list, tuple, None]:
    """Remove veto(s) from targets."""
    start = strax.to_str_tuple(targets)
    remove = strax.to_str_tuple(remove)
    if targets is None:
        return None
    for r in remove:
        targets = keep_target(targets, {f"*{r}": 0}, 1)
    if _flip:
        targets = [t for i, t in enumerate(start) if not np.in1d(start, targets)[i]]
    return strax.to_str_tuple(targets)


def _keep_veto_from_t(
    targets: ty.Union[str, list, tuple],
    keep: ty.Union[str, list, tuple] = "_nv",
) -> ty.Union[str, list, tuple, None]:
    """Remove non-veto(s) targets."""
    targets = _remove_veto_from_t(targets, remove=keep, _flip=True)  # type: ignore
    if not len(targets):
        targets = strax.to_str_tuple("raw_records")
    return targets


def keep_target(targets, compare_with, n_fails):
    kept_targets = []
    delete_after = -1  # just to make logging never fail below
    for target_name in strax.to_str_tuple(targets):
        for delete_target, delete_after in compare_with.items():
            failed_too_much = n_fails > delete_after
            name_matches = fnmatch.fnmatch(target_name, delete_target)
            if failed_too_much and name_matches:
                log.warning(f"remove {target_name} ({n_fails}>{delete_after})")
                break
        else:
            log.debug(f"keep {target_name} ({n_fails}!>{delete_after})")
            kept_targets.append(target_name)
    if not len(kept_targets):
        kept_targets = ["raw_records"]
    return kept_targets


def infer_target(rd: dict) -> dict:
    """Check if the target should be overridden based on the mode of the DAQ for this run.

    :param rd: rundoc
    :return: dict with the targets and the targets for post processing

    """
    targets = args.targets.copy()
    post_process = args.post_process.copy()

    if args.fix_target:
        return {
            "targets": strax.to_str_tuple(targets),
            "post_processing": strax.to_str_tuple(post_process),
        }

    n_fails = rd["bootstrax"].get("n_failures", 0)

    if n_fails:
        log.debug(f"Deleting targets")
        targets = keep_target(targets, remove_target_after_fails, n_fails)
        post_process = keep_target(post_process, remove_target_after_fails, n_fails)

    log.debug(f"{targets} and {post_process} remaining")

    # Special modes override target for these
    led_modes = ["pmtgain"]
    diagnostic_modes = [
        "exttrig",
        "noise",
        "mv_diffuserballs",
        "mv_fibres",
        "mv_darkrate",
    ]
    ap_modes = ["pmtap"]
    nv_ref_mon = [
        "nVeto_LASER_calibration",
    ]
    mode = str(rd.get("mode"))
    detectors = list(rd.get("detectors"))  # type: ignore

    log.debug(f"mode is {mode}, changing target if needed")
    if np.any([m in mode for m in led_modes]):
        log.debug("led-mode")
        targets = "led_calibration"
        post_process = "raw_records"
    elif np.any([m in mode for m in ap_modes]):
        log.debug("afterpulse mode")
        targets = "afterpulses"
        post_process = "raw_records"
    elif np.any([m in mode for m in nv_ref_mon]):
        log.debug("NV reflecitvity and diffuser ball mode")
        targets = "ref_mon_nv"
        post_process = "raw_records"
    elif np.any([m in mode for m in diagnostic_modes]):
        log.debug("diagnostic-mode")
        targets = "raw_records"
        post_process = "raw_records"
    elif "kr83m" in mode and (len(targets) or len(post_process)):
        # Override the first (highest level) plugin for Kr runs (could
        # also use source field, outcome is the same)
        if "event_info" in targets or "event_info" in post_process:
            targets = list(targets) + ["event_info_double"]
    elif "ambe" in mode:
        # rates are very high, to ensure smooth operation let's just do this
        # based on calibrations of Apr 2023 this is the only safe working solution
        log.debug("ambe-mode")

        # get the mode from the daq_db
        # this is a new thing from Nov 2023
        # it overwrites the mode from the rundb
        bootstrax_config_coll = daq_db["bootstrax_config"]
        bootstrax_config = bootstrax_config_coll.find_one({"name": "bootstrax_config"})

        this_eb_ambe_mode = bootstrax_config["ambe_modes"].get(hostname[:3], "default")
        log.debug(f"Ambe mode for {hostname} is {this_eb_ambe_mode}")

        if this_eb_ambe_mode != "default":
            log.debug(f"Overwriting targets and post processing for {hostname} from daq_db")
            targets = bootstrax_config["modes_definitions"][this_eb_ambe_mode]["targets"]
            post_process = bootstrax_config["modes_definitions"][this_eb_ambe_mode]["post_process"]

    targets = strax.to_str_tuple(targets)
    post_process = strax.to_str_tuple(post_process)

    if "tpc" not in detectors:
        keep = []
        if "neutron_veto" in detectors:
            keep += ["_nv"]
        if "muon_veto" in detectors:
            keep += ["_mv"]
        targets = _keep_veto_from_t(targets, keep=keep)
        post_process = _keep_veto_from_t(post_process, keep=keep)
    else:
        for det, remove in (("neutron_veto", "_nv"), ("muon_veto", "_mv")):
            if det not in detectors:
                # Remove the _veto if this detector is not in the detector list
                targets = _remove_veto_from_t(targets, remove=remove)
                post_process = _remove_veto_from_t(post_process, remove=remove)
        if len(detectors) > 1:
            log.info(
                f'{rd["number"]:06} running in linked mode ({detectors}), '
                f"processing up to {targets} and postprocessing "
                f"to {post_process}"
            )

    if targets is None or not len(targets):
        targets = "raw_records"
    if post_process is None or not len(post_process):
        post_process = "raw_records"

    targets = strax.to_str_tuple(targets)
    post_process = strax.to_str_tuple(post_process)
    log.info(f"Inferring modes done, writing {targets} and {post_process}")
    for check in (targets, post_process):
        if not len(set(check)) == len(check):
            log_warning(f"Duplicates in (post) targets {check}", priority="fatal")
            raise ValueError(f"Duplicates in (post) targets {check}")

    return {"targets": targets, "post_processing": post_process}


def set_state(state, update_fields=None):
    """Inform the bootstrax collection we're in a different state.

    if state is None, leave state unchanged, just update heartbeat time

    """
    # Find the last message of this host
    previous_entry = bs_coll.find_one({"host": hostname}, sort=[("_id", pymongo.DESCENDING)])
    if state is None:
        state = "None" if previous_entry is None else previous_entry.get("state")

    bootstrax_state = dict(
        host=hostname,
        pid=os.getpid(),
        time=now(),
        state=state,
        targets=args.targets,
        max_cores=args.cores,
        max_messages=args.max_messages,
        undying=args.undying,
        production_mode=args.production,
    )
    if update_fields:
        update_fields = strax.storage.mongo.remove_np(update_fields)
        bootstrax_state.update(update_fields)

    need_new_doc = (previous_entry is None) or (
        (now() - previous_entry["time"].replace(tzinfo=pytz.utc)).seconds
        > timeouts["min_status_interval"]
    )
    if need_new_doc:
        bs_coll.insert_one(bootstrax_state)
    else:
        bs_coll.update_one({"_id": previous_entry["_id"]}, {"$set": bootstrax_state})


def send_heartbeat(update_fields=None):
    """Inform the bootstrax collection we're still here Use during long-running tasks where state
    doesn't change."""
    # Same as set_state, just don't change state
    set_state(None, update_fields=update_fields)


def log_warning(message, priority="warning", run_id=None):
    getattr(log, priority.lower())(message)
    databases.log_warning(
        message,
        priority=priority,
        run_id=run_id,
        production=args.production,
        user=f"bootstrax_{hostname}",
    )


def eb_can_process():
    """The new ebs (eb3-5) should be sufficient to process all data. In exceptional circumstances
    eb3-5 cannot keep up. Only let eb0-2 also process data in such cases.

    Before eb0-2 are also used for processing two criteria have to be fulfilled:
        - There should be runs waiting to be processed
        - Eb3-5 should be busy processing for a substantial time.
    :return: bool if this host should process a run

    """

    # eb3-5 always process.
    if hostname in ["eb3.xenon.local", "eb4.xenon.local", "eb5.xenon.local"]:
        return True

    # In test mode we can always process
    if not args.production:
        return True
    elif "eb2" in hostname:
        log_warning("Why is eb2 alive?!", priority="error")
        return False

    # Count number of runs untouched by bootstrax.
    n_untouched_runs = run_coll.count_documents({"bootstrax.state": None})

    # Check that eb3-5 are all busy for at least some time.
    n_ebs_running = 0
    n_ebs_busy = 0
    for eb_i in range(3, 6):
        # Should count if eb3-5 are registered as running (as one might be offline).
        bootstrax_on_host = bs_coll.find_one(
            {
                "host": f"eb{eb_i}.xenon.local",
                "time": {"$gt": now(-timeouts["bootstrax_presumed_dead"])},
            },
            sort=[("time", pymongo.DESCENDING)],
        )

        if bootstrax_on_host:
            n_ebs_running += 1
            running_eb = run_coll.find_one(
                {
                    "bootstrax.state": "busy",
                    "bootstrax.host": f"eb{eb_i}.xenon.local",
                    "bootstrax.started_processing": {"$lt": now(-timeouts["eb3-5_max_busy_time"])},
                }
            )
            if running_eb:
                n_ebs_busy += 1
                log.debug(f"eb{eb_i} is busy")
    log.info(f"running: {n_ebs_running}\tbusy: {n_ebs_busy}\tqueue: {n_untouched_runs}")
    if not n_ebs_running:
        return True
    if n_untouched_runs > max_queue_new_runs:
        return True
    return False


def infer_mode(rd):
    """Infer a safe operating mode of running bootstrax based on the uncompressed redax rate.

    Estimating save parameters for running
    bootstrax from:
    https://xe1t-wiki.lngs.infn.it/doku.php?id=xenon:xenonnt:dsg:daq:eb_speed_tests_2021update
    :return: dictionary of how many cores, max_messages and compressor
    should be used based on an estimated data rate.

    """
    # Get data rate from dispatcher
    data_rate = 0
    if "rate" in rd:
        # When a run finishes, the rundb is updated with the rates by the
        # dispatcher. Especially useful if the aggregate status does not
        # have the run anymore.
        data_rate = float(sum([detector["max"] for detector in rd["rate"].values()]))
    try:
        started_looking = time.time()
        time_to_wait = (
            timeouts["min_data_rate_infer_time"]
            - (now() - rd["start"].replace(tzinfo=timezone.utc)).total_seconds()
        )
        if time_to_wait > 0:
            log.debug(f"Waiting {time_to_wait:.1f} s to infer datarate")
            time.sleep(time_to_wait)
        while data_rate == 0:
            # For runs that are still running, we should be able to get the
            # info from the aggregate status collection.
            docs = ag_stat_coll.aggregate(
                [
                    {"$match": {"number": rd["number"]}},
                    {"$group": {"_id": "$detector", "rate": {"$max": "$rate"}}},
                ]
            )
            data_rate = float(sum([d["rate"] for d in docs]))
            if data_rate > 0:
                break
            elif time.time() - started_looking > timeouts["max_data_rate_infer_time"]:
                raise RuntimeError(f'Could not infer_mode for {rd["number"]}')
            log.debug(
                f'No rate inferred for {rd["number"]} after {time.time() - started_looking:.1f} s.'
            )
            time.sleep(2)

    except Exception as e:
        log_warning(
            f"infer_mode ran into {e}. Cannot infer datarate, using default mode.",
            run_id=f'{rd["number"]:06}',
            priority="warning",
        )
        data_rate = 0

    # Find out if eb is new (eb3-eb5):
    is_new_eb = int(hostname[2]) >= 3  # ebX.xenon.local
    log.info(f"Data rate: {data_rate:.1f} MB/s. New_eb: {is_new_eb}")
    benchmark = {
        "mbs": [0, 70, 90, 110, 150, 220, 290, 360, 390, 420, 500, 550],
        "cores_old": [39, 35, 35, 30, 30, 20, 12, 12, 10, 10, 10, 8],
        "cores": [24, 24, 24, 24, 18, 15, 15, 15, 15, 15, 15, 10],
        "max_messages_old": [20, 20, 15, 15, 10, 10, 10, 10, 10, 10, 8, 6],
        "max_messages": [60, 60, 35, 30, 25, 25, 25, 25, 20, 15, 12, 12],
        "timeout": [1200, 1200, None, None, None, None, None, None, None, None, None, 2400],
    }
    if data_rate and args.infer_mode:

        # Temporary solution
        # It is a patch to try to process some ambe data without failing for memory
        # If we are doing ambe -> consider the maximum rate
        # so that we have 10 cores and 12 messages for new ebs
        # and we have     8 cores and 6 messages for old ebs
        # added by Carlo on 19 April 2023
        mode = str(rd.get("mode"))
        if "ambe" in mode:
            data_rate = 550

        df = pd.DataFrame(benchmark)
        if data_rate not in benchmark["mbs"]:
            df.loc[len(df.index)] = [data_rate, None, None, None, None, None]
        df.set_index("mbs", inplace=True)
        df.sort_values("mbs", inplace=True)
        df.interpolate(method="index", inplace=True)
        result = {k: int(v) for k, v in df.loc[data_rate].items()}
        if not is_new_eb:
            for k in ("cores", "max_messages"):
                result[k] = result[k + "_old"]
        del df, benchmark, result["cores_old"], result["max_messages_old"]
    else:
        result = dict(cores=args.cores, max_messages=args.max_messages, timeout=1000)

    n_fails = rd["bootstrax"].get("n_failures", 0)
    if args.fix_resources:
        # If we are in a fix resource mode, we should not change the resources
        # based on the number of failures.
        n_fails = 0
        log.debug(f"Fixing resources, ignoring {n_fails} previous failures")

    if n_fails:
        # Exponentially lower resources & increase timeout
        result = dict(
            cores=np.clip(result["cores"] / (1.1**n_fails), 4, 40).astype(int),
            max_messages=np.clip(result["max_messages"] / (1.1**n_fails), 4, 100).astype(int),
            timeout=np.clip(result["timeout"] * (1.1**n_fails), 500, 3600).astype(int),
        )
        log_warning(
            f'Repeated failures on {rd["number"]}@{hostname}. Lowering to {result}',
            priority="info",
            run_id=f'{rd["number"]:06}',
        )
    else:
        result = {k: int(v) for k, v in result.items()}
    result["records_compressor"] = infer_records_compressor(rd, data_rate, n_fails)
    log.info(f'Inferred mode for {rd["number"]}\t{result}')
    return result


def infer_records_compressor(rd, datarate, n_fails):
    """
    Get a compressor for the (raw)records. This takes two things in consideration:
    1. Do we store the data fast enough (high write speed)
    2. Does the data fit into the buffer

    Used compressors:
        bz2: slow but very good compression -> use for low datarate
        zstd: fast & decent compression, max chunk size of ??? GB
        lz4: fast & not no chunk size limit, use if all ese fails
    """
    if n_fails or datarate is None:
        # Cannot infer datarate or failed before, go for fast & safe
        return "lz4" if n_fails > 1 else "zstd"

    chunk_length = rd["daq_config"]["strax_chunk_overlap"] + rd["daq_config"]["strax_chunk_length"]
    chunk_size_mb = datarate * chunk_length
    if datarate < 65:
        # Low data rate, we can do very large compression
        return "zstd"
    if chunk_size_mb > 1800:
        # Extremely large chunks, let's use LZ4 because we know that it
        # can handle this.
        return "lz4"
    # High datarate and reasonable chunk size.
    return "zstd"


##
# Host interactions
##


def sufficient_diskspace():
    """Check if there is sufficient space available on the local disk to write to."""
    for i in range(wait_diskspace_n_max):
        du = disk_usage(output_folder)
        disk_pct = du.percent
        disk_free = du.free / (1024**4)
        if disk_pct < wait_diskspace_max_space_percent and disk_free > min_disk_space_tb:
            log.debug(f"Check disk space: {disk_pct:.1f}% full")
            # Sufficient space to write to, let's continue
            return
        if i == 0:
            # Log it once to the database, the first time. Otherwise, just log it locally
            log_warning(
                f"Insufficient free disk space ({disk_pct:.1f}% full) on {hostname}. "
                f"Waiting {i}/{wait_diskspace_n_max}",
                priority="warning",
            )
        else:
            log.warning(f"Insufficient free disk space ({disk_pct:.1f}% full)")
        time.sleep(wait_diskspace_dt)
        send_heartbeat(dict(state="disk full"))
    set_state(dead_state)
    message = f"No disk space to write to. Kill bootstrax on {hostname}"
    log_warning(message, priority="fatal")
    raise RuntimeError(message)


def delete_live_data(rd, live_data_path):
    """Open thread to delete the live_data."""
    if args.production and os.path.exists(live_data_path) and args.delete_live:
        delete_thread = threading.Thread(
            name=delete_thread_name, target=_delete_data, args=(rd, live_data_path, "live")
        )
        log.info(f"Starting thread to delete {live_data_path} at {now()}")
        # We rather not stop deleting the live_data if something else
        # fails. Set the thread to daemon.
        delete_thread.setDaemon(True)
        delete_thread.start()
        log.info(
            f"DeleteThread {live_data_path} should be running in parallel, "
            f"continue MainThread now: {now()}"
        )


def _delete_data(rd, path, data_type):
    """After completing the processing and updating the RunDB, remove the live_data."""

    if data_type == "live" and not args.delete_live and args.production:
        message = "Unsafe operation. Trying to delete live data!"
        log_warning(message, priority="fatal")
        raise ValueError(message)
    log.debug(f"Deleting data at {path}")
    if os.path.exists(path):
        shutil.rmtree(path)
    log.info(f"deleting {path} finished")
    # Remove the data location from the rundoc and append it to the 'deleted_data' entries
    if not os.path.exists(path):
        log.info("changing data field in rundoc")
        for ddoc in rd["data"]:
            if ddoc["type"] == data_type:
                break
        for k in ddoc.copy().keys():
            if k in ["location", "meta", "protocol"]:
                ddoc.pop(k)

        ddoc.update({"at": now(), "by": hostname})
        log.debug(f"update with {ddoc}")
        run_coll.update_one(
            {"_id": rd["_id"]},
            {
                "$addToSet": {"deleted_data": ddoc},
                "$pull": {"data": {"type": data_type, "host": {"$in": ["daq", hostname]}}},
            },
        )
    else:
        message = f"Something went wrong we wanted to delete {path}!"
        log_warning(message, priority="fatal")
        raise ValueError(message)


def wait_on_delete_thread():
    """Check that the thread with the delete_thread_name is finished before continuing."""
    threads = threading.enumerate()
    for thread in threads:
        if thread.name == delete_thread_name:
            done = False
            while not done:
                if thread.is_alive():
                    log.info(f'{thread.name} still running take a {timeouts["idle_nap"]} s nap')
                    time.sleep(timeouts["idle_nap"])
                    done = True
    log.info(f"Checked that {delete_thread_name} finished")


def clear_shm():
    """Manually delete files in /dev/shm/ created by npshmex on starup."""
    shm_dir = "/dev/shm/"
    shm_files = [f for f in os.listdir(shm_dir) if "npshmex" in f]

    if not len(shm_files):
        return
    log.info(f"Clearing {len(shm_files)} files")
    for f in shm_files:
        os.remove(shm_dir + f)


##
# Run DB interaction
##


def ping_dbs():
    while True:
        try:
            run_db.command("ping")
            daq_db.command("ping")
            break
        except Exception as ping_error:
            log.warning(
                f"Failed to connect to Mongo. Ran into {ping_error}. Sleep for a minute.",
                priority="warning",
            )
            time.sleep(60)


def get_run(*, mongo_id=None, number=None, full_doc=False):
    """Find and return run doc matching mongo_id or number The bootstrax state is left unchanged.

    :param full_doc: If true (default is False), return the full run doc rather than just fields
        used by bootstrax.

    """
    if number is not None:
        query = {"number": number}
    elif mongo_id is not None:
        query = {"_id": mongo_id}
    else:
        # This means you are not running a normal bootstrax (no reason to report to rundb)
        raise ValueError("Please give mongo_id or number")

    return run_coll.find_one(query, projection=None if full_doc else bootstrax_projection)


def set_run_state(rd, state, return_new_doc=True, **kwargs):
    """Set state of run doc rd to state
    return_new_doc: if True (default), returns new document.
        if False, instead returns the original (un-updated) doc.

    Any additional kwargs will be added to the bootstrax field.
    """
    if not args.production:
        return run_coll.find_one({"_id": rd["_id"]})

    bd = rd["bootstrax"]
    bd.update({"state": state, "host": hostname, "time": now(), **kwargs})

    if state == "failed":
        bd["n_failures"] = bd.get("n_failures", 0) + 1

    return run_coll.find_one_and_update(
        {"_id": rd["_id"]},
        {"$set": {"bootstrax": bd}},
        return_document=return_new_doc,
        projection=bootstrax_projection,
    )


def check_data_written(rd):
    """Checks that the data as written in the runs-database is actually available on this machine.

    :param rd: rundoc
    :return: type bool, False if not all paths exist or if there are no files on this host.

    """
    files_written = 0

    # Fetch the rd again -> to see status of 'data' field
    new_rd = get_run(mongo_id=rd["_id"], full_doc=True)
    log.debug(new_rd["data"])
    for ddoc in new_rd["data"]:
        ddoc_loc = ddoc.get("location", "NO LOCATION")
        ddoc_host = ddoc.get("host", "NO HOST")
        log.debug(f"Checking {ddoc_loc} on {ddoc_host} (current {hostname})")
        if ddoc_host == hostname:
            log.debug(f"Counting files {ddoc_loc} on {ddoc_host}")
            if os.path.exists(ddoc_loc):
                log.debug(f"{ddoc_loc} written")
                files_written += 1
            else:
                log.info(f"No data at {ddoc_loc}")
                return False
    log.info(f"{files_written} files are saved")
    return files_written > 0


def all_files_saved(rd, wait_max=600, wait_per_cycle=10):
    """Check that all files are written. It might be that the savers are still in the process of
    renaming from folder_temp to folder. Hence allow some wait time to allow the savers to finish.

    :param rd: rundoc
    :param wait_max: max seconds to wait for data to save
    :param wait_per_cycle: wait this many seconds if the data is not yet there

    """
    start = time.time()
    while not check_data_written(rd):
        log.debug(f'{rd["number"]} not all saved')
        if time.time() - start > wait_max:
            log_warning(f'Not all files saved for {rd["number"]}@{hostname}?!', priority="warning")
            return False
        send_heartbeat()
        time.sleep(wait_per_cycle)
    return True


def get_end_time(rd) -> typing.Optional[datetime]:
    return run_coll.find_one({"_id": rd["_id"]}, projection={"end": 1}).get("end", None)


def set_status_finished(rd):
    """Set the status to ready to upload for datamanager and admix."""
    # Check mongo connection
    ping_dbs()

    if not args.production:
        # Don't update the status if we are not in production mode
        return

    # First check that all the data is available (that e.g. no _temp files
    # are being renamed). This line should be over-redundant as we already
    # check earlier.
    all_files_saved(rd)

    # Only update the status if it does not exist or if it needs to be uploaded
    ready_for_restrax = {"status": "eb_finished_pre"}
    if rd.get("status") in [None, "needs_upload"]:
        run_coll.update_one({"_id": rd["_id"]}, {"$set": ready_for_restrax})
    elif rd.get("status") == ready_for_restrax.get("status"):
        # This is strange, bootstrax already finished this run before
        log_warning(
            "WARNING: bootstax has already marked this run as ready for restrax. Doing nothing.",
            priority="warning",
            run_id=f'{rd["number"]:06}',
        )
    else:
        # Do not override this field for runs already uploaded in admix
        message = (
            f'Trying to set set the status {rd.get("status")} to '
            f"{ready_for_restrax}! One should not override this field."
        )
        log_warning(message, priority="fatal")
        raise ValueError(message)


def abandon(*, mongo_id=None, number=None):
    """Mark a run as abandoned."""
    set_run_state(get_run(mongo_id=mongo_id, number=number), "abandoned")


def consider_run(query, return_new_doc=True, test_counter=0):
    """Return one run doc matching query, and simultaneously set its bootstrax state to
    'considering'."""
    # We must first do an atomic find-and-update to set the run's state
    # to "considering", to ensure the run doesn't get picked up by a
    # bootstrax on another host.
    if args.production:
        rd = run_coll.find_one_and_update(
            query,
            {"$set": {"bootstrax.state": "considering"}},
            projection=bootstrax_projection,
            return_document=True,
            sort=[("start", pymongo.DESCENDING)],
        )
        # Next, we can update the bootstrax entry properly with set_run_state
        # (adding hostname, time, etc.)
        if rd is None:
            return None
        return set_run_state(rd, "considering", return_new_doc=return_new_doc)
    else:
        # Don't change the runs-database for test modes
        try:
            rds = run_coll.find(
                query, projection=bootstrax_projection, sort=[("start", pymongo.DESCENDING)]
            )
            return rds[test_counter]
        except IndexError:
            return None


def fail_run(rd, reason, error_traceback=""):
    """Mark the run represented by run doc rd as failed with reason."""
    if "number" not in rd:
        long_run_id = f"run <no run number!!?>:{rd['_id']}"
    else:
        long_run_id = f"run {rd['number']}"

    # No bootstrax info is present when manually failing a run with args.fail
    if "bootstrax" not in rd.keys():
        rd["bootstrax"] = {}
        rd["bootstrax"]["n_failures"] = 0

    if (rd["bootstrax"].get("n_failures", 0) > 0) or (
        "perhaps it crashed on this run or is still stuck" in reason
    ):
        fail_name = "Repeated failure"
        failure_message_level = "info"
    else:
        fail_name = "New failure"
        failure_message_level = "warning"

    # Cleanup any data associated with the run
    # TODO: This should become optional, or just not happen at all,
    # after we're done testing (however, then we need some other
    # pruning mechanism like AJAX!)
    clean_run(mongo_id=rd["_id"])

    # Report to run db
    # It's best to do this after everything is done;
    # as it changes the run state back away from 'considering', so another
    # bootstrax could conceivably pick it up again.
    set_run_state(
        rd,
        "failed",
        reason=f"{hostname}:\n{reason}\n{error_traceback}",
        next_retry=(
            now(
                plus=(
                    timeouts["retry_run"]
                    * np.random.uniform(0.5, 1.5)
                    # Exponential backoff with jitter
                    * 5 ** min(rd["bootstrax"].get("n_failures", 0), 3)
                )
            )
        ),
    )

    # Report to DAQ log and screen. Let's not also add the entire traceback
    log_warning(
        f"{fail_name} on {long_run_id}: {reason}",
        priority=failure_message_level,
        run_id=f'{rd["number"]:06}',
    )


def manual_fail(*, mongo_id=None, number=None, reason=""):
    """Manually mark a run as failed based on mongo_id or run number."""
    rd = get_run(mongo_id=mongo_id, number=number)
    fail_run(rd, "Manually set failed state. " + reason)


##
# Processing
##


def run_strax(
    run_id,
    input_dir,
    targets,
    readout_threads,
    compressor,
    run_start_time,
    samples_per_record,
    cores,
    max_messages,
    timeout,
    daq_chunk_duration,
    daq_overlap_chunk_duration,
    post_processing,
    records_compressor,
    debug=False,
):
    # Check mongo connection
    ping_dbs()
    # Clear the swap memory used by npshmmex
    npshmex.shm_clear()
    # double check by forcefully clearing shm
    clear_shm()

    if debug:
        logging.basicConfig(force=True)
        logging.getLogger().setLevel(logging.DEBUG)
    try:
        log.info(f"Starting strax to make {run_id} with input dir {input_dir}")

        if targets == strax.to_str_tuple("led_calibration"):
            # TODO: still true?
            # timeout *= 5
            pass

        # Create multiple targets
        st = new_context(
            cores=cores,
            max_messages=max_messages,
            timeout=timeout,
        )

        for t in ("raw_records", "records", "records_nv", "hitlets_nv"):
            # Set the (raw)records processor to the inferred one
            st._plugin_class_registry[t].compressor = records_compressor

        # Make a function for running strax, call the function to process the run
        # This way, it can also be run inside a wrapper to profile strax
        def st_make():
            """Run strax."""
            strax_config = dict(
                daq_input_dir=input_dir,
                daq_compressor=compressor,
                run_start_time=run_start_time,
                record_length=samples_per_record,
                daq_chunk_duration=daq_chunk_duration,
                daq_overlap_chunk_duration=daq_overlap_chunk_duration,
                readout_threads=readout_threads,
                check_raw_record_overlaps=True,
            )
            log.info(f"Making {run_id}-{targets}")
            log.debug(f"With {strax_config}, n-cores {cores}")
            st.make(run_id, targets, allow_multiple=True, config=strax_config, max_workers=cores)

            if len(post_processing):
                for post_target in post_processing:
                    if post_target not in st._plugin_class_registry:
                        log_warning(
                            f"Trying to make unknown data type {post_target}",
                            priority="error",
                            run_id=run_id,
                        )
                        continue
                    elif not st.is_stored(run_id, post_target):
                        log.info(f"Making {post_target}")
                        st.make(
                            run_id,
                            post_target,
                            config=strax_config,
                            progress_bar=True,
                            max_workers=cores,
                        )
                    else:
                        log.info(f"Not making {post_target}, it is already stored")

        if args.profile.lower() == "false":
            st_make()
        else:
            prof_file = f"run{run_id}_{args.profile}"
            if ".prof" not in prof_file:
                prof_file += ".prof"
            log.info(f"starting with profiler, saving as {prof_file}")
            with strax.profile_threaded(prof_file):
                st_make()
    except Exception as e:
        log.warning(f"Ran into {e} while processing {run_id}", exc_info=True)

        # Write exception to file, so bootstrax can read it
        exc_info = strax.formatted_exception()
        log.warning(f"Uploading traceback {exc_info}")
        with open(exception_tempfile, mode="w") as f:
            f.write(exc_info)
        os.makedirs("./bootstrax_exceptions", exist_ok=True)
        with open(f"./bootstrax_exceptions/{run_id}_exception.txt", mode="w") as f:
            f.write(exc_info)
        raise


def last_file_write_time(match_location: str) -> datetime:
    """Get the datetime in UTC of the last file that was written in raw-records*

    :param match_location: Location where runs are written to
    :return: TZ aware timestamp.

    """
    last_time = datetime.fromtimestamp(0).replace(tzinfo=pytz.utc)
    matched_folders = glob(match_location)
    for folder in matched_folders:
        # check last three files only
        chunk_files = sorted(glob(os.path.join(folder, "*")))[-3:]
        for chunk in chunk_files:
            # Check that we did not rename this file since the glob above
            if os.path.exists(chunk):
                chunk_write_time = datetime.fromtimestamp(os.stat(chunk).st_mtime).replace(
                    tzinfo=pytz.utc
                )
                last_time = max(last_time, chunk_write_time)
    return last_time


def process_run(rd, send_heartbeats=args.production):
    log.info(f"Starting processing of run {rd['number']}")
    if rd is None:
        raise RuntimeError("Pass a valid rundoc, not None!")
    elif args.production and rd.get("bootstrax", {}).get("state", None) == "done":
        raise RuntimeError(f'{rd["number"]} is done already, do not make a mass')

    # Shortcuts for failing
    class RunFailed(Exception):
        pass

    def fail(reason, **kwargs):
        if args.production:
            fail_run(rd, reason, **kwargs)
        else:
            log.warning(reason)
        raise RunFailed

    try:
        try:
            run_id = "%06d" % rd["number"]
        except Exception as e:
            fail(f"Could not format run number: {str(e)}")

        if not args.production:
            # We are just testing let's assume its on the usual location
            loc = osp.join("/live_data/xenonnt/", run_id)
            # or use the test-dir:
            if not osp.exists(loc):
                loc = os.path.join("/live_data/xenonnt_bootstrax_test/", run_id)

        else:
            for dd in rd["data"]:
                if "type" not in dd:
                    fail("Corrupted data doc, found entry without 'type' field")
                if dd["type"] == "live":
                    break
                else:
                    fail("Non-live data already registered; untracked failure?")
            loc = osp.join(dd["location"], run_id)
        if not osp.exists(loc):
            fail(f"No live data at claimed location {loc}")

        run_strax_config = dict(run_id=run_id, input_dir=loc)

        if "daq_config" not in rd:
            fail("No daq_config in the rundoc!")
        try:
            # Fetch parameters from the rundoc. If not readable, let's use redax' default
            # values (that are hardcoded here).
            dq_conf = rd["daq_config"]
            to_read = (
                "processing_threads",
                "strax_chunk_length",
                "strax_chunk_overlap",
                "strax_fragment_payload_bytes",
                "compressor",
            )
            report_missing_config = [conf for conf in to_read if conf not in dq_conf]
            if report_missing_config:
                log_warning(
                    f'{", ".join(report_missing_config)} not in rundoc for '
                    f"{run_id}! Using default values.",
                    priority="warning",
                    run_id=run_id,
                )
            run_strax_config["readout_threads"] = dq_conf.get("processing_threads", None)
            run_strax_config["daq_chunk_duration"] = int(dq_conf.get("strax_chunk_length", 5) * 1e9)
            run_strax_config["daq_overlap_chunk_duration"] = int(
                dq_conf.get("strax_chunk_overlap", 0.5) * 1e9
            )
            # note that value in rd in bytes hence //2
            run_strax_config["samples_per_record"] = (
                dq_conf.get("strax_fragment_payload_bytes", 220) // 2
            )
            run_strax_config["compressor"] = dq_conf.get("compressor", "lz4")
        except Exception as e:
            fail(f"Could not find {to_read} in rundoc: {str(e)}")

        if run_strax_config["readout_threads"] is None:
            fail(f"Run doc for {run_id} has no readout thread count info")

        # Remove any previous processed data
        # If we do not do this, strax will just load this instead of
        # starting a new processing
        if args.production:
            clean_run(mongo_id=rd["_id"])
        else:
            clean_run_test_data(run_id)

        # Remove any temporary exception info from previous runs
        if osp.exists(exception_tempfile):
            os.remove(exception_tempfile)

        if not args.production and "bootstrax" not in rd:
            # Bootstrax does not register in non-production mode
            pass
        try:
            run_strax_config["run_start_time"] = (
                rd["start"].replace(tzinfo=timezone.utc).timestamp()
            )
        except Exception as e:
            fail(f"Could not find start in datetime.datetime object: {str(e)}")

        run_strax_config.update(infer_target(rd))
        run_strax_config.update(infer_mode(rd))
        run_strax_config["debug"] = args.debug
        strax_proc = multiprocessing.Process(target=run_strax, kwargs=run_strax_config)

        t0 = now()
        info = dict(started_processing=t0)
        strax_proc.start()

        while True:
            if send_heartbeats:
                to_report = [
                    "run_id",
                    "targets",
                    "cores",
                    "max_messages",
                    "timeout",
                    "post_processing",
                ]
                update = {k: v for k, v in run_strax_config.items() if k in to_report}
                send_heartbeat(update)
            ec = strax_proc.exitcode
            if ec is None:
                # fail(bla) raises RunFailed so no need for elifs. Make sure to
                # kill main process before raising errors in fail(bla)

                # Fail because we are taking a very long time to process
                if t0 < now(-timeouts["max_processing_time"]):
                    kill_process(strax_proc.pid)
                    fail(f"Processing took longer than {timeouts['max_processing_time']} sec")

                # Fail because we are taking a very long time to process
                # despite the run having ended
                endtime = get_end_time(rd)
                if (
                    endtime is not None
                    and endtime.replace(tzinfo=pytz.utc)
                    < now(-timeouts["max_processing_time_after_completion"])
                    and t0 < now(-timeouts["max_processing_time_after_completion"])
                ):
                    kill_process(strax_proc.pid)
                    fail(
                        "After the run ended, processing did not succeed in "
                        f"{timeouts['max_processing_time_after_completion']} sec."
                    )

                # Fail because for some reason, we are not writing any new files to disk.
                if t0 < now(-timeouts["max_no_write_time"]) and (
                    last_write := last_file_write_time(os.path.join(output_folder, f"{run_id}*"))
                ) < now(-timeouts["max_no_write_time"]):
                    kill_process(strax_proc.pid)
                    fail(
                        f"No data written for {timeouts['max_no_write_time']} sec. Last write is"
                        f" from {last_write}"
                    )

                if args.production:
                    set_run_state(rd, "busy", **info)
                time.sleep(timeouts["check_on_strax"])
                log.info(f"Still processing run {run_id}. PID:{strax_proc.pid}")
                continue

            elif ec == 0:
                log.info(f"Strax done on run {run_id}, performing basic data quality check")
                if args.ignore_checks:
                    # I hope you know what you are doing, we are not going to
                    # do any of the checks below.
                    # Make sure to fetch the latest rundoc
                    rd = get_run(mongo_id=rd["_id"])
                else:
                    log.info(f"Open metadata")
                    try:
                        # Sometimes we have only he channels or mv channels,
                        # try loading one until we get one with chunks.
                        for rr_type in (
                            "raw_records",
                            "raw_records_he",
                            "raw_records_mv",
                            "raw_records_nv",
                        ):
                            md = st.get_metadata(run_id, rr_type)
                            if len(md["chunks"]) and (
                                "first_time" in md["chunks"][0]
                                and "last_endtime" in md["chunks"][0]
                            ):
                                log.info(f"Using {rr_type}-metadata")
                                break
                    except Exception:
                        fail(
                            "Processing succeeded, but metadata not readable",
                            error_traceback=traceback.format_exc(),
                        )
                    if not len(md["chunks"]):
                        fail("Processing succeeded, but no chunks were written!")

                    if any(
                        (chunk["n"] and "filesize" in chunk and not chunk["filesize"])
                        for chunk in md["chunks"]
                    ):
                        # E.g. you tried compressing >2 GB chunk using blosc
                        fail("At least one chunk failed writing!")

                    log.info(f"Check that run has ended")
                    rd = get_run(mongo_id=rd["_id"])
                    end_time = rd.get("end")
                    if end_time is None:
                        fail("Processing succeeded, but run hasn't yet ended!")

                    log.info(f"Check the processing time of the run")
                    # Check that the data written covers the run
                    # (at least up to some fudge factor)
                    # Since chunks can be empty, and we don't want to crash,
                    # this has to be done with some care...
                    # Lets assume some ridiculous timestamp (in ns): 10e9*1e9
                    t_covered = timedelta(
                        seconds=(
                            max([x.get("last_endtime", 0) for x in md["chunks"]])
                            - min([x.get("first_time", 10e9 * 1e9) for x in md["chunks"]])
                        )
                        / 1e9
                    )
                    log.info(f"Compute runtime")
                    run_duration = end_time - rd["start"]
                    if not (0 < t_covered.total_seconds() < float("inf")):
                        fail(f"Processed data covers {t_covered} sec")
                    if not (
                        timedelta(seconds=-max_timetamp_diff)
                        < (run_duration - t_covered)
                        < timedelta(seconds=max_timetamp_diff)
                    ):
                        fail(
                            f"Processing covered {t_covered.total_seconds()}, "
                            f"but run lasted {run_duration.total_seconds()}!"
                        )
                    if args.production:
                        # Only check rundoc for files written in production mode
                        log.info(f"Check files saved")
                        if not all_files_saved(rd):
                            fail("Not all files in the rundoc for this run are saved")

                log.info(f"Run {run_id} processed successfully")
                if args.production:
                    set_run_state(rd, "done", **info)

                    set_status_finished(rd)

                    if args.delete_live:
                        delete_live_data(rd, loc)
                break

            else:
                # This is just the info that we're starting
                # exception retrieval. The actual error comes later.
                log.info(f"Failure while processing run {run_id}")
                if osp.exists(exception_tempfile):
                    with open(exception_tempfile, mode="r") as f:
                        exc_info = f.read()
                    if not exc_info:
                        exc_info = "[No exception info known, exception file was empty?!]"
                else:
                    exc_info = "[No exception info known, exception file not found?!]"
                fail(
                    f"Strax exited with exit code {ec}.",
                    error_traceback=f"Exception info: {exc_info}",
                )
    except RunFailed:
        return


##
# Cleanup
##


def clean_run(*, mongo_id=None, number=None, force=False):
    """Removes all data on this host associated with a run that was previously registered in the run
    db.

    Does NOT remove temporary folders, nor data that isn't registered to the run db.

    """
    # We need to get the full data docs here, since I was too lazy to write
    # a surgical update below
    rd = get_run(mongo_id=mongo_id, number=number, full_doc=True)
    have_live_data = False
    for dd in rd["data"]:
        if dd["type"] == "live":
            have_live_data = True
            break
    for ddoc in rd["data"]:
        if "host" in ddoc and ddoc["host"] == hostname:
            loc = ddoc["location"]
            if not force and not have_live_data and "raw_records" in ddoc["type"]:
                log.info(
                    f"prevent {loc} from being deleted. The live_data has already been removed"
                )
            elif os.path.exists(loc):
                log.info(f"delete data at {loc}")
                _delete_data(rd, loc, ddoc["type"])
            else:
                loc = loc + "_temp"
                log.info(f"delete data at {loc}")
                _delete_data(rd, loc, ddoc["type"])

    # Also wipe the online_monitor if there is any
    run_db["online_monitor"].delete_many({"number": int(rd["number"])})


def clean_run_test_data(run_id):
    """Clean the data in the test_data_folder associated with this run_id."""
    for folder in os.listdir(test_data_folder):
        if run_id in folder:
            log.info(f"Cleaning {test_data_folder + folder}")
            shutil.rmtree(test_data_folder + folder)


def cleanup_db():
    """Find various pathological runs and clean them from the db.

    Also cleans the bootstrax collection for stale entries

    """
    # Check mongo connection
    ping_dbs()

    log.info("Checking for bad stuff in database")

    # Check for all the ebs if their last state message is not longer
    # ago than the time we assume that the eb is dead.
    for eb_i in range(6):
        bd = bs_coll.find_one(
            {"host": f"eb{eb_i}.xenon.local"}, sort=[("time", pymongo.DESCENDING)]
        )
        if (
            bd
            and bd["time"].replace(tzinfo=pytz.utc) < now(-timeouts["bootstrax_presumed_dead"])
            and bd["state"] is not dead_state
        ):
            bs_coll.find_one_and_update({"_id": bd["_id"]}, {"$set": {"state": dead_state}})

    # Runs that say they are 'considering' or 'busy' but nothing happened for a while
    for state, timeout in [
        ("considering", timeouts["max_considering_time"]),
        ("busy", timeouts["max_busy_time"]),
    ]:
        while True:
            send_heartbeat()
            rd = consider_run(
                {"bootstrax.state": state, "bootstrax.time": {"$lt": now(-timeout)}},
                return_new_doc=False,
            )
            if rd is None:
                break
            fail_run(
                rd,
                f"Host {rd['bootstrax']['host']} said it was {state} "
                f"at {rd['bootstrax']['time']}, but then didn't get further; "
                "perhaps it crashed on this run or is still stuck?",
            )

    # Runs for which, based on the run doc alone, we can tell they are in a bad state
    # Mark them as failed.
    failure_queries = [
        (
            {"bootstrax.state": "done", "end": None},
            "Bootstrax state was done, but run did not yet end",
        ),
        (
            {
                "bootstrax.state": "done",
                "data": {"$not": {"$elemMatch": {"type": {"$ne": "live"}}}},
            },
            "Bootstrax state was done, but no processed data registered",
        ),
    ]

    for query, failure_message in failure_queries:
        while True:
            send_heartbeat()
            rd = consider_run(query)
            if rd is None:
                break
            fail_run(rd, failure_message.format(**rd))

    # Abandon runs which we already know are so bad that
    # there is no point in retrying them
    abandon_queries = [
        (
            {
                "tags.name": "abandon",
                "bootstrax.state": "done",
                "start": {"$gt": now(-timeouts["abandoning_allowed"])},
            },
            "Run has an 'abandon' tag",
        ),
        (
            {"tags.name": "abandon", "bootstrax.state": "failed"},
            "Run has an 'abandon' tag and was failing",
        ),
    ]

    for query, failure_message in abandon_queries:
        failure_message += " -- run has been abandoned"
        while True:
            send_heartbeat()
            rd = consider_run(query)
            if rd is None:
                break
            fail_run(rd, failure_message.format(**rd))
            abandon(mongo_id=rd["_id"])


def main():
    if not args.undying:
        run()
    else:
        while True:
            try:
                run()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as fatal_error:
                log.error(
                    f"Fatal warning:\tran into {fatal_error}. Try "
                    "logging error and restart bootstrax"
                )
                try:
                    log_warning(f"Fatal warning:\tran into {fatal_error}", priority="error")
                except Exception as warning_error:
                    log.error(f"Fatal warning:\tcould not log {warning_error}")
                # This usually only takes a minute or two
                time.sleep(60)
                log.warning("Restarting run loop")


if __name__ == "__main__":
    main()
