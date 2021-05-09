#!/usr/bin/env python
import argparse

from pathlib import Path
from subprocess import check_call, CalledProcessError
from sys import platform, exit

from nspyre.errors import OSNotSupportedError

from .mongo_listener import (
    Synched_Mongo_Database
)

__all__ = [
    'Synched_Mongo_Database'
]


THIS_DIR = Path(__file__).parent


def main():
    """Entry point for mongodb CLI"""
    # parse command-line arguments
    arg_parser = argparse.ArgumentParser(prog='nspyre-mongodb', description='Start / restart the MongoDB server')
    cmd_args = arg_parser.parse_args()
    try:
        if platform == 'linux' or platform == 'linux2' or platform == 'darwin':
            check_call(['bash', str(THIS_DIR / 'start_mongo_unix.sh')])
        elif platform == 'win32':
            check_call([str(THIS_DIR / 'start_mongo_win.bat')])
        else:
            raise OSNotSupportedError('Your OS [{}] is not supported'.\
                                        format(platform))
    except CalledProcessError:
        exit(1)

    exit(0)


if __name__ == '__main__':
    main()
