"""Common utility functions."""
import json
import logging
import re
from datetime import datetime
from itertools import product
from os import environ as env
from os.path import join, dirname, expanduser
from sys import argv
from typing import Any

_LOGGER = logging.getLogger(__name__)


def read_config() -> dict:
    """Read config from file."""
    for directory, filename in product(
        [
            dirname(argv[0]),
            expanduser("~"),
            env.get("XDG_CONFIG_HOME", join(expanduser("~"), ".config")),
        ],
        ["vw.conf", ".vw.conf"],
    ):
        try:
            config = join(directory, filename)
            _LOGGER.debug("checking for config file %s", config)
            with open(config) as config:
                return dict(x.split(": ") for x in config.read().strip().splitlines() if not x.startswith("#"))
        except OSError:
            continue
    return {}


def json_loads(s) -> Any:
    """Load JSON from string and parse timestamps."""
    return json.loads(s, object_hook=obj_parser)


def obj_parser(obj: dict) -> dict:
    """Parse datetime."""
    for key, val in obj.items():
        try:
            obj[key] = datetime.strptime(val, "%Y-%m-%dT%H:%M:%S%z")
        except (TypeError, ValueError):
            """The value was not a date."""
    return obj


def find_path(src, path) -> Any:
    """
    Return data at path in source.

    Simple navigation of a hierarchical dict structure using XPATH-like syntax.

    >>> find_path(dict(a=1), 'a')
    1

    >>> find_path(dict(a=1), '')
    {'a': 1}

    >>> find_path(dict(a=None), 'a')


    >>> find_path(dict(a=1), 'b')
    Traceback (most recent call last):
    ...
    KeyError: 'b'

    >>> find_path(dict(a=dict(b=1)), 'a.b')
    1

    >>> find_path(dict(a=dict(b=1)), 'a')
    {'b': 1}

    >>> find_path(dict(a=dict(b=1)), 'a.c')
    Traceback (most recent call last):
    ...
    KeyError: 'c'

    """
    if not path:
        return src
    if isinstance(path, str):
        path = path.split(".")
    return find_path(src[path[0]], path[1:])


def is_valid_path(src, path):
    """
    Check if path exists in source.

    >>> is_valid_path(dict(a=1), 'a')
    True

    >>> is_valid_path(dict(a=1), '')
    True

    >>> is_valid_path(dict(a=1), None)
    True

    >>> is_valid_path(dict(a=1), 'b')
    False
    """
    try:
        find_path(src, path)
        return True
    except KeyError:
        return False


def camel2slug(s: str) -> str:
    """Convert camelCase to camel_case.

    >>> camel2slug('fooBar')
    'foo_bar'
    """
    return re.sub("([A-Z])", "_\\1", s).lower().lstrip("_")
