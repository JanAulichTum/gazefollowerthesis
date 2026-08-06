# -*- coding: utf-8 -*-
"""
pygazetracker – browser-based pupil tracking for Flask experiments.

Adapted from esdalmaijer/webcam-eyetracker, rewritten for Python 3
without PyGame dependencies.  Receives webcam frames as base64 JPEG
from the browser via SocketIO and performs server-side pupil detection.
"""

from .tracker import PupilTracker

__all__ = ["PupilTracker"]
