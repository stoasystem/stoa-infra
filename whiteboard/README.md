# Classroom whiteboard (WBO) image

The virtual classroom (stoa-frontend#696, stoa-backend#467) frames a self-hosted
[WBO](https://github.com/lovasoa/whitebophir) board. This folder builds the image
that runs it on hosts that refuse binaries with file capabilities, such as Render:
the upstream `lovasoa/wbo` image gives `node` `cap_net_bind_service` so that its
non-root user can bind port 80, and those hosts fail with
`exec /usr/local/bin/node: Operation not permitted`. The Dockerfile copies the
binary (dropping the capability) and listens on port 10000.

- Base image pinned by digest; bump it on purpose.
- `AUTH_SECRET_KEY` (set on the host, never here) must equal the backend's
  `WBO_AUTH_SECRET`: the backend signs each board's JWT with it.
- No volume: boards live only as long as the instance, which is intended.
- Run one instance only; WBO does not support several writers.
- `/favicon.ico` answers 200 without a token, for uptime checks.
