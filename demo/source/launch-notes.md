# Launch Notes

- Launch timing is tied to the Oct 31 Ghost Core review batch.
- A/B pricing variants are final: $79 anchor and $149 anchor (site/?v=a|b).
- Email capture is still a stub: the backend is an open launch gate.
- The wow moment for the demo: ask the assistant about the calendar and have it
  tell you what is coming up, fully local.

# Config Snapshot Notes

- Port mapping: Steward daemon 127.0.0.1:3877. The SP REST daemon keeps 3876.
- The app talks to the daemon over EXPO_PUBLIC_STEWARD_DAEMON_URL.
- The data source folder is configurable via STEWARD_DEMO_SOURCE; point it at
  the Obsidian vault or any notes folder for real use.
