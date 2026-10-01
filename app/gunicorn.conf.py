"""Recover streaming child processes during worker shutdown."""
import sys


def close_streams():
    module = sys.modules.get("frontend")
    if module:
        module.system.streams.close()


def worker_exit(server, worker):
    close_streams()


def worker_int(worker):
    close_streams()


def worker_abort(worker):
    close_streams()
