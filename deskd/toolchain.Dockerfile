# The image deskd's tests run in: the Go of go.mod, a virtual X display to drive, and xdotool, whose
# input stands in for the operator's own hand (it is not the daemon's, so the daemon must notice it).
FROM golang:1.26
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xdotool x11-utils \
    && rm -rf /var/lib/apt/lists/*
