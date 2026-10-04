# Daedalus runtime image. The agent may edit this file; a change triggers an image rebuild.
#
# Five stages. The environment is assembled where a compiler, a Node and a Go are allowed to live,
# and only the results — the virtualenv, the built Mini App and the two daemons — are copied into
# the image that ships.
# The `speech` extra rides in the default image rather than being an opt-in like `browser`: the two
# wheels together are about fifteen megabytes against Playwright's several hundred, and an installation
# that never downloads a model never loads them. opus-tools comes in with it, from apt-packages.txt,
# because a Telegram voice note is Ogg Opus and a model reads samples: opusdec is one and a quarter
# megabytes and decodes exactly that. ffmpeg would decode everything else too and is deliberately not
# here — it drags four hundred and fifty megabytes of video codecs in behind it, to convert a voice clip.
#
# Two targets come out of it: `runtime` (the default, and what `:latest` is) and `browser`, which
# adds Playwright, the headless Chromium shell the browser skills drive, and the full Chromium the
# `browser` compose service runs under browserd. They share every layer up to the last one, so
# having both costs the difference, not twice the image.

FROM ubuntu:24.04 AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/venv \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/srv/venv/bin:/usr/local/bin:${PATH}"
# UV_PYTHON + UV_PYTHON_DOWNLOADS: the interpreter is the one apt already installed. Left to itself
# uv downloads a second CPython of its own next to it, which nothing ever runs.

COPY deploy/apt-packages.txt /tmp/apt-packages.txt
RUN apt-get update \
    && xargs -a /tmp/apt-packages.txt apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/info /usr/share/lintian /usr/share/perl/*/pod

# uv builds and syncs the environment; it stays in the image because the supervisor's preflight runs
# `uv sync` when the agent changes a dependency. uvx goes: nothing in the image runs a tool from an
# ephemeral environment any more.
# No Node, no GitHub CLI, no compiler — the first two build the Mini App and the third builds a
# wheel, and both of those happen in a builder stage.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh \
    && rm -f /usr/local/bin/uvx \
    && git config --system --add safe.directory '*'


# -- the virtualenv ---------------------------------------------------------------------------
# Built from the lock file alone, so the stage needs neither checkout. The two path dependencies —
# the bot itself and the core — are left out here and reach the interpreter as source paths in the
# final stage: they are bind-mounted at run time and the agent edits them, so an installed copy
# would be a stale second copy.
FROM base AS venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml uv.lock ./
# The last step stamps the environment with what it was built for. The runtime mounts a volume over
# /srv/venv and Docker seeds it from here, so the first start finds the stamp already matching the checkout
# and runs no uv at all; a checkout that later declares different dependencies no longer matches it, and
# the supervisor syncs before it starts the bot.
RUN uv venv /srv/venv \
    && uv export --frozen --extra speech --no-emit-project --no-emit-package protocore --format requirements-txt -o /build/requirements.txt \
    && uv export --frozen --extra browser --no-emit-project --no-emit-package protocore --format requirements-txt -o /build/requirements-browser.txt \
    && uv pip install --python /srv/venv/bin/python --no-cache --requirements /build/requirements.txt \
    && python3 -c "import hashlib, pathlib; d = hashlib.sha256(); [d.update(pathlib.Path(n).read_bytes()) for n in ('uv.lock', 'pyproject.toml')]; pathlib.Path('/srv/venv/.daedalus-dependencies').write_text(d.hexdigest())"


# -- the Mini App -----------------------------------------------------------------------------
# The bundle is not in git, and the runtime image has no Node to build one. Built here and baked in;
# the supervisor copies it into a checkout that has none.
FROM node:22-slim AS miniapp

WORKDIR /build/miniapp
COPY miniapp/package.json miniapp/package-lock.json ./
RUN npm ci --no-audit --no-fund
# The type check covers the app's tests too, and one of them reads the daemon's own fixtures from
# beside the app (../ptyd), so the terminal and the daemon are held to the same recorded screens.
# Without these three files `tsc -b` fails and no bundle is built.
COPY ptyd/internal/emulator/ghostty/testdata/snapshots.json /build/ptyd/internal/emulator/ghostty/testdata/
COPY ptyd/internal/emulator/conformance/testdata/cases.json /build/ptyd/internal/emulator/conformance/testdata/
COPY ptyd/internal/answer/testdata/xterm-replies.json /build/ptyd/internal/answer/testdata/
# The companion's stage is shared with the mascot studio and the launcher's setup page, and the app
# imports it from beside itself; without these the bundle cannot resolve ../../mascot-studio.
COPY mascot-studio/*.js /build/mascot-studio/
COPY desktop/ui/assets/setup/stage3d.js desktop/ui/assets/setup/chibi2.js /build/desktop/ui/assets/setup/
COPY desktop/ui/assets/setup/vendor /build/desktop/ui/assets/setup/vendor
COPY miniapp ./
RUN npm run build


# -- the terminal daemon ----------------------------------------------------------------------
# ptyd owns the terminals. It is Go with a C library linked into it — libghostty-vt, the screen
# emulator, which Zig builds from the sources ptyd/libghostty/pins.env names — and neither toolchain
# belongs in the image that ships, so they live in this stage and only the one binary leaves it.
# The binary lands in the same image as the agent because the `terminals` service in
# deploy/compose.yaml is that image started with ptyd as its entrypoint: one image to pull, and the
# daemon is always the build the host beside it was tested against.
#
# Debian bookworm's Go rather than the default tag's newer Debian: the library is static but the C
# runtime is not, and a binary linked against a newer glibc than Ubuntu 24.04's refuses to start.
#
# The stage runs on the machine that builds and compiles for the one the image is for. The published
# image is built for arm64 as well, and Zig building Ghostty under an emulated CPU is many times
# slower than a cross build, which costs nothing extra here: Zig targets any architecture on its own,
# and the Go side needs only Debian's cross C compiler for its few lines of cgo.
FROM --platform=$BUILDPLATFORM golang:1.26-bookworm AS ptyd
ARG TARGETARCH

RUN case "$TARGETARCH" in \
        amd64) triple=x86_64-linux-gnu ;; \
        arm64) triple=aarch64-linux-gnu ;; \
        *) echo "no terminal daemon build for $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && cross="" \
    && if [ "$TARGETARCH" != "$(dpkg --print-architecture)" ]; then cross="gcc-$triple libc6-dev-$TARGETARCH-cross"; fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends xz-utils $cross \
    && rm -rf /var/lib/apt/lists/* \
    && echo "$triple" > /opt/target-triple

WORKDIR /src/ptyd
# The library first, from the pins and the patches alone, so the minutes of Zig are paid again only
# when one of those moves, never for a change to the daemon's Go. Only the installed library is kept:
# the compiler, the Ghostty checkout and Zig's caches are a gigabyte nothing after this step reads.
COPY ptyd/libghostty ./libghostty
RUN PTYD_CACHE=/opt/libghostty LIBGHOSTTY_TARGET="$(cat /opt/target-triple)" libghostty/build.sh > /opt/libghostty.pkgconfig \
    && find /opt/libghostty -mindepth 1 -maxdepth 1 ! -name 'libghostty-*' -exec rm -rf {} +

COPY ptyd/go.mod ptyd/go.sum ./
RUN go mod download
COPY ptyd ./
# The version is a digest of the daemon's sources. The host compares the version of the daemon that
# is running with the one in its own image, and the digest differs exactly when the daemon changed —
# a rebuild of the agent for any other reason does not offer to end every terminal for nothing.
# The binary is asked its version only when this machine can run it.
RUN export PKG_CONFIG_PATH="$(cat /opt/libghostty.pkgconfig)" \
    && triple="$(cat /opt/target-triple)" \
    && cc=gcc \
    && if [ "$TARGETARCH" != "$(dpkg --print-architecture)" ]; then cc="$triple-gcc"; fi \
    && digest="$(find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)" \
    && CGO_ENABLED=1 GOOS=linux GOARCH="$TARGETARCH" CC="$cc" go build -trimpath \
        -ldflags "-s -w -X github.com/ascorblack/daedalus/ptyd/internal/version.Version=src-$digest" \
        -o /out/ptyd ./cmd/ptyd \
    && if [ "$cc" = gcc ]; then /out/ptyd version; fi


# -- the browser daemon -----------------------------------------------------------------------
# browserd owns the agent's browsers (docs/architecture/browser.md). It is plain Go with no C in it,
# so it cross-compiles for the image's architecture with nothing but the Go toolchain. It builds
# against ptyd's shared packages through a replace directive, so the daemon's own module and
# ptyd/proto both come in; ptyd's emulator and its library do not, and a change to them does not
# rebuild this stage.
FROM --platform=$BUILDPLATFORM golang:1.26-bookworm AS browserd
ARG TARGETARCH

WORKDIR /src/browserd
COPY ptyd/go.mod ptyd/go.sum /src/ptyd/
COPY browserd/go.mod browserd/go.sum ./
RUN go mod download
COPY ptyd/proto /src/ptyd/proto
COPY browserd ./
# The version is a digest of what the binary is built from: its own sources, ptyd's shared packages
# and the module files. The host compares it with the running daemon's, and the digest differs
# exactly when the daemon changed, so a rebuild of the agent for any other reason never offers to
# end every browser for nothing. browserd/release.sh computes the same digest over a checkout, so a
# release's daemon and an image's of the same sources report one version.
RUN digest="$(cd /src && find browserd ptyd/proto ptyd/go.mod ptyd/go.sum -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)" \
    && CGO_ENABLED=0 GOOS=linux GOARCH="$TARGETARCH" go build -trimpath \
        -ldflags "-s -w -X github.com/ascorblack/daedalus/browserd/internal/version.Version=src-$digest" \
        -o /out/browserd ./cmd/browserd \
    && if [ "$TARGETARCH" = "$(go env GOHOSTARCH)" ]; then /out/browserd version; fi


# -- what ships -------------------------------------------------------------------------------
FROM base AS runtime

COPY --from=venv /srv/venv /srv/venv
COPY --from=miniapp /build/miniapp/dist /opt/miniapp-dist
# The terminals service runs this as its entrypoint. The agent's container carries it too, and only
# asks it its version: that is how the host knows a newer daemon is in the image than the one running.
COPY --from=ptyd /out/ptyd /usr/local/bin/ptyd
# The same for the browser daemon: the `browser` service runs it, and the agent asks the copy in its
# own image for its version to know when the running one is older.
COPY --from=browserd /out/browserd /usr/local/bin/browserd
# The run directory the agent and the browser service share (the browser-run volume), owned by the
# browser's user. Docker seeds an empty named volume from the first container that mounts it, and
# that may be the agent's: made here, the volume starts owned by the user browserd runs as, whichever
# container comes first. Root in the agent's container reads it all the same.
RUN install -d -m 0700 -o 1001 -g 1001 /run/daedalus-browser
# The bot and the core are mounted, not installed: pointing the interpreter at the mounted checkouts
# is what an editable install would have written anyway, and it needs no sync at the first start.
RUN printf '%s\n' /srv/daedalus /srv/protocore-exp > /srv/venv/lib/python3.12/site-packages/_daedalus_checkouts.pth

# The supervisor lives outside the repositories the agent can change.
COPY launcher /opt/launcher
COPY deploy/dependencies /opt/dependency-recipe
RUN python3 /opt/launcher/dependencies.py /opt/dependency-recipe/local.json
# The key proxy runs from this image as a second container with its own environment; see
# deploy/compose.yaml. Its dependencies are already in the virtualenv above.
COPY deploy/keyproxy /opt/keyproxy

WORKDIR /srv/daedalus
# The virtualenv lives outside the mounted checkout so host and container never share one.
ENV DAEDALUS_VENV_EXTRAS=speech
ENV DAEDALUS_BOT_REPO=/srv/daedalus \
    DAEDALUS_CORE_REPO=/srv/protocore-exp \
    DAEDALUS_STATE=/srv/state \
    DAEDALUS_WORKSPACES=/srv/workspaces \
    DAEDALUS_SUPERVISOR_SOCKET=/run/daedalus/supervisor.sock \
    DAEDALUS_BAKED_APP=/opt/miniapp-dist \
    DAEDALUS_BOT_CMD="/srv/venv/bin/python -m daedalus serve"

ENTRYPOINT ["python3", "/opt/launcher/supervisor.py"]


# -- the browser variant ----------------------------------------------------------------------
# `:browser` = the image above plus Playwright and two builds of Chromium, both Playwright's pinned
# ones, fetched by one `playwright install chromium`:
#   - the headless shell, which the browser skills drive: Playwright's `launch(headless=True)` looks
#     for exactly that build, and with only the full one installed it fails (measured, 1.62);
#   - the full Chromium, which browserd runs in the `browser` compose service with --headless=new.
#     The shell is Chromium's //content without the browser around it; the screencast, the
#     accessibility tree and trusted input the agent's browser rests on were measured on the full
#     build, and the full build is what a site sees as Chrome.
# The browsers live outside the virtualenv so a re-sync of the environment never re-downloads them.
FROM runtime AS browser

ENV DAEDALUS_VENV_EXTRAS=speech,browser
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
COPY --from=venv /build/requirements-browser.txt /tmp/requirements-browser.txt
COPY deploy/apt-packages-browser.txt /tmp/apt-packages-browser.txt
RUN apt-get update \
    && grep -v '^#' /tmp/apt-packages-browser.txt | xargs apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man \
    && uv pip install --python /srv/venv/bin/python --no-cache --requirements /tmp/requirements-browser.txt \
    && playwright install chromium \
    && rm -rf /root/.cache \
    && shell="$(find /opt/pw-browsers -type f \( -name chrome-headless-shell -o -name headless_shell \) | head -1)" \
    && test -n "$shell" \
    && ln -s "$shell" /usr/local/bin/chromium \
    && test -n "$(find /opt/pw-browsers -path '*/chromium-*/chrome-linux*/chrome' -type f | head -1)"
ENV CHROME_PATH=/usr/local/bin/chromium
# The user the `browser` service runs browserd and Chromium as. Chromium's own sandbox gives every
# renderer a user namespace of its own, which it can make as an ordinary user once the container's
# seccomp profile allows it (deploy/compose.yaml) — no added capability, and never --no-sandbox as
# root. Its home and state directory are made here so a fresh volume is seeded with the right owner.
RUN useradd --uid 1001 --user-group --home-dir /var/lib/browserd --no-create-home --shell /usr/sbin/nologin browser \
    && install -d -m 0700 -o browser -g browser /var/lib/browserd
