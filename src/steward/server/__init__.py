"""Steward daemon server package: pairing auth, SSE chat, conversations, mDNS.

Modules:
    auth.py    6-digit pairing code, HMAC session tokens, pair rate limiting
    store.py   SQLite conversation and message store (WAL)
    mdns.py    zeroconf advertisement of _steward._tcp.local
    daemon.py  the hardened HTTP server (see README.md for every route)
"""
