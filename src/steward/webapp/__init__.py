"""Steward webapp: the in-box client, served by the daemon at the root.

The client is static HTML, CSS and JS in this package plus a thin API
surface in serve.py. Nothing needs a build step, a CDN, or a network
connection: the daemon serves every byte from this directory.
"""

VERSION = "0.3.0"
