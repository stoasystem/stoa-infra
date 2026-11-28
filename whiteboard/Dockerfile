# The WBO whiteboard for hosts that refuse binaries with file capabilities
# (Render). The upstream image gives /usr/local/bin/node cap_net_bind_service
# so that its non-root user can listen on port 80; such hosts then fail with
# "exec /usr/local/bin/node: Operation not permitted". A plain copy of the
# binary drops the capability, and the board listens on an unprivileged port.
FROM lovasoa/wbo@sha256:4d6f55fedd867120326e7bad1b73106ce23c822c376db9a2376bea8a6b6c7993

USER root
RUN cp /usr/local/bin/node /tmp/node \
 && mv /tmp/node /usr/local/bin/node \
 && chmod 0755 /usr/local/bin/node
USER 1000:1000

# Render routes to the port the service listens on; 10000 is its default.
ENV PORT=10000
EXPOSE 10000
