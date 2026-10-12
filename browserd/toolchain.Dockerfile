# The image browserd's tests run in: the Go of go.mod, and a Chromium to drive. Debian's chromium is
# not the build a release pins (Playwright's), but it speaks the same protocol, and a test that
# passes on both is the point; BROWSERD_CHROMIUM names another one when it is mounted in. Xvfb is
# here for the headed mode the container runs: BROWSERD_TEST_MODE=headed runs the same tests with
# windows on a display the daemon starts.
FROM golang:1.26
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation xvfb \
    && rm -rf /var/lib/apt/lists/*
ENV BROWSERD_CHROMIUM=/usr/lib/chromium/chromium
