from __future__ import annotations

import os

from flask import Flask


def create_app():
    return Flask("app")


def create_app2(foo, bar):
    return Flask("_".join(["app2", foo, bar]))


def no_app():
    pass


def create_env_app():
    """Factory that reads ``MY_VAR`` from the environment and uses it as
    the app name, so tests can verify which env file was loaded.
    """
    name = os.environ.get("MY_VAR", "default")
    return Flask(name)
