#!/usr/bin/env bash
# First-run setup: asks for what the stack needs, writes .env and the secrets file outside the checkout,
# and starts the containers. Safe to re-run: existing values are offered as defaults and never printed back
# in full. Run from the repository root: bash deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ENV_FILE="$ROOT/.env"
SECRETS_DIR="$(cd .. && pwd)/daedalus-secrets"
SECRETS_FILE="$SECRETS_DIR/keyproxy.env"
CORE_DIR="$(cd .. && pwd)/protocore-exp"

say() { printf '\n%s\n' "$*"; }
current() { grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- || true; }
ask() {  # ask VAR "prompt" [secret] — keeps the current value on an empty answer
  local var="$1" prompt="$2" secret="${3:-}" file="${4:-$ENV_FILE}" cur ans shown
  cur="$(current "$var" "$file")"
  if [ -n "$cur" ]; then shown="$cur"; [ -n "$secret" ] && shown="${cur:0:4}…${cur: -3}"; prompt="$prompt [$shown]"; fi
  if [ -n "$secret" ]; then read -r -s -p "$prompt: " ans; echo; else read -r -p "$prompt: " ans; fi
  ans="${ans:-$cur}"
  if grep -qE "^$var=" "$file" 2>/dev/null; then
    python3 - "$file" "$var" "$ans" <<'PY'
import sys, pathlib, re
path, var, value = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); text = p.read_text()
p.write_text(re.sub(rf"^{re.escape(var)}=.*$", lambda m: f"{var}={value}", text, count=1, flags=re.M))
PY
  else
    printf '%s=%s\n' "$var" "$ans" >> "$file"
  fi
}

say "Daedalus setup. Values go to $ENV_FILE and $SECRETS_FILE."
command -v docker >/dev/null || { echo "docker is not installed"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose is not available"; exit 1; }
[ -d "$CORE_DIR" ] || { say "The core checkout is missing: cloning protocore-exp next to this repository."; git clone -q https://github.com/anchor-inference/protocore-exp "$CORE_DIR"; }
[ -t 0 ] || { echo "setup.sh asks questions: run it in a terminal"; exit 1; }
[ -f "$ENV_FILE" ] || cp deploy/env.example "$ENV_FILE"
chmod 600 "$ENV_FILE"
mkdir -p "$SECRETS_DIR" && chmod 700 "$SECRETS_DIR"
[ -f "$SECRETS_FILE" ] || cp deploy/keyproxy.env.example "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"

say "1/5 Telegram — optional. Leave the token empty to run on the app alone; you will sign in with a pairing link the bot writes at startup."
say "(https://t.me/BotFather for the token; @userinfobot for your numeric id; https://my.telegram.org/apps for the API pair)"
ask TELEGRAM_BOT_TOKEN "Bot token (empty = no Telegram)" secret
if [ -n "$(current TELEGRAM_BOT_TOKEN "$ENV_FILE")" ]; then
  ask OWNER_USER_ID "Your numeric Telegram user id"
  ask TELEGRAM_API_ID "Telegram API id"
  ask TELEGRAM_API_HASH "Telegram API hash" secret
fi

say "2/5 Provider keys (stored in $SECRETS_FILE, never inside the checkout)."
say "A key is an address, not a model: you pick the model itself in the app at the end, and it is the only step that cannot be skipped."
say "API: 1 DeepSeek · 2 OpenRouter · 3 OpenAI · 4 Z.AI · 5 MiniMax · 6 Moonshot AI"
say "Subscription: 7 OpenCode Go · 8 Z.AI Coding Plan · 9 MiniMax Token Plan · 10 Kimi Code"
say "Enter a number to add or change a key; press Enter when finished."
while true; do
  read -r -p "Provider number (Enter to finish): " choice
  case "$choice" in
    "") break ;;
    1) ask DEEPSEEK_API_KEY "DeepSeek API key" secret "$SECRETS_FILE" ;;
    2) ask OPENROUTER_API_KEY "OpenRouter API key" secret "$SECRETS_FILE" ;;
    3) ask OPENAI_API_KEY "OpenAI API key" secret "$SECRETS_FILE" ;;
    4) ask ZAI_API_KEY "Z.AI API key" secret "$SECRETS_FILE" ;;
    5) ask MINIMAX_API_KEY "MiniMax API key" secret "$SECRETS_FILE" ;;
    6) ask MOONSHOT_API_KEY "Moonshot AI API key" secret "$SECRETS_FILE" ;;
    7) ask OPENCODE_API_KEY "OpenCode Go key" secret "$SECRETS_FILE" ;;
    8) ask ZAI_CODING_API_KEY "Z.AI Coding Plan key" secret "$SECRETS_FILE" ;;
    9) ask MINIMAX_PLAN_API_KEY "MiniMax Token Plan key" secret "$SECRETS_FILE" ;;
    10) ask KIMI_CODING_API_KEY "Kimi Code key" secret "$SECRETS_FILE" ;;
    *) say "Choose 1–10, or Enter to finish." ;;
  esac
done
ask KEYPROXY_USD_PER_DAY "Daily spend cap for the key proxy, USD" "" "$SECRETS_FILE"
say "Calls with no known model price do not count toward a USD cap; subscriptions also have their own provider quotas."

say "3/5 GitHub and the app"
ask GITHUB_TOKEN "Fine-grained GitHub token for the two agent repositories (pull requests + contents); empty disables self-development" secret
ask MINIAPP_PUBLIC_URL "Public HTTPS address of the app (a reverse proxy in front of port 8765); empty for LAN only"
ask USD_PER_DAY "Daily spend cap enforced by the supervisor, USD"
if [ -z "$(current SEARXNG_SECRET "$ENV_FILE")" ]; then
  python3 - "$ENV_FILE" <<'PY'
import sys, pathlib, secrets, re
p = pathlib.Path(sys.argv[1]); text = p.read_text()
p.write_text(re.sub(r"^SEARXNG_SECRET=.*$", f"SEARXNG_SECRET={secrets.token_hex(24)}", text, count=1, flags=re.M))
PY
fi
python3 - "$ENV_FILE" "$ROOT" "$CORE_DIR" <<'PY'
import sys, pathlib, re
path, root, core = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); text = p.read_text()
for var, value in (("DAEDALUS_COMPOSE_PROJECT_DIR", root), ("DAEDALUS_COMPOSE_FILE", f"{root}/deploy/compose.yaml"), ("DAEDALUS_CORE_PROJECT_DIR", core)):
    text = re.sub(rf"^{var}=.*$", f"{var}={value}", text, count=1, flags=re.M) if re.search(rf"^{var}=", text, re.M) else text + f"\n{var}={value}"
p.write_text(text)
PY

# The host terminal's run directory is mounted into the agent whether or not the host terminal is
# installed, so it must exist before compose starts: Docker would create a missing one as root, and
# the unit, which runs as you, could then never write to it.
HOST_TERMINALS_DIR="$(bash deploy/host-terminal.sh run-dir)"
if ! bash deploy/host-terminal.sh prepare-dir; then
  read -r -p "Hand it over now with sudo chown? [y/N]: " answer
  case "$answer" in
    y | Y | yes | YES) sudo chown "$(id -un):" "$HOST_TERMINALS_DIR" && bash deploy/host-terminal.sh prepare-dir ;;
    *) say "Left as it is; the host terminal cannot be installed until it is yours: sudo chown $(id -un): \"$HOST_TERMINALS_DIR\"" ;;
  esac
fi

say "4/5 Starting the stack (the first build takes a few minutes)."
PROFILE=()
[ -n "$(current TELEGRAM_BOT_TOKEN "$ENV_FILE")" ] && PROFILE=(--profile telegram)  # the local Bot API server is only for a bot
STATE_COMPOSE=()
if [ "$(current DAEDALUS_ENCRYPTED_STATE_REQUIRED "$ENV_FILE")" = 1 ]; then
  bash deploy/encrypted-state.sh check \
    "$(current DAEDALUS_ENCRYPTED_STATE_DIR "$ENV_FILE")" \
    "$(current DAEDALUS_ENCRYPTED_STATE_SOURCE "$ENV_FILE")" \
    "$(current DAEDALUS_ENCRYPTED_STATE_ID "$ENV_FILE")"
  # An existing named state volume may hold the installation's only database. Enabling the bind
  # here would hide that volume and appear to start a fresh installation without migrating it.
  STATE_VOLUME=$(docker compose -f deploy/compose.yaml --env-file "$ENV_FILE" config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["volumes"]["daedalus-state"]["name"])')
  if docker volume inspect "$STATE_VOLUME" >/dev/null 2>&1; then
    echo "existing named state volume requires a separate, verified migration before encrypted setup" >&2
    exit 1
  fi
  STATE_COMPOSE=(-f deploy/compose.encrypted-state.yaml)
elif [ -n "$(current DAEDALUS_ENCRYPTED_STATE_DIR "$ENV_FILE")" ] || \
     [ -n "$(current DAEDALUS_ENCRYPTED_STATE_SOURCE "$ENV_FILE")" ] || \
     [ -n "$(current DAEDALUS_ENCRYPTED_STATE_ID "$ENV_FILE")" ]; then
  echo "encrypted state settings are set without the required mount guard" >&2
  exit 1
fi
docker compose -f deploy/compose.yaml "${STATE_COMPOSE[@]}" --env-file "$ENV_FILE" "${PROFILE[@]}" up -d --build
say "Done. Logs: docker logs -f deploy-daedalus-1"

say "5/5 Host terminal — optional."
if systemctl --user cat daedalus-ptyd.service >/dev/null 2>&1; then
  say "The host terminal is installed. Updating it copies this build's daemon to the server and restarts it, which ends every open host terminal."
  read -r -p "Update the host terminal? [y/N]: " answer
  case "$answer" in y | Y | yes | YES) bash deploy/host-terminal.sh install --yes || say "The host terminal was not updated; see above." ;; esac
else
  say "A shell on this server as you, opened from the app. Every attach and every agent write is recorded; what you type is not."
  say "It runs as a systemd user unit of yours, and can be removed with: systemctl --user disable --now daedalus-ptyd"
  read -r -p "Install the host terminal? [y/N]: " answer
  case "$answer" in y | Y | yes | YES) bash deploy/host-terminal.sh install --yes || say "The host terminal was not installed; see above. Try again later with: bash deploy/host-terminal.sh install" ;; esac
fi
if [ ${#PROFILE[@]} -gt 0 ]; then
  say "Next: send /start to the bot; /bind in a supergroup with topics for parallel sessions; /app for the Mini App."
  say "Then add a model in the app (it opens on \"Add a model\"): until one exists, nothing can run."
else
  say "Open the app with the pairing link the bot wrote at startup (or mint a fresh one):"
  say "  docker exec deploy-daedalus-1 cat /srv/state/pairing-url"
  say "  docker exec deploy-daedalus-1 /srv/venv/bin/python -m daedalus auth pair"
  say "A fresh link: docker compose -f deploy/compose.yaml exec daedalus python -m daedalus auth pair"
  say "Add a passkey in Settings → Security once you are in, and the link is never needed again."
  say "The app opens on \"Add a model\": pick a provider, pick a model from its own list, save. Until one exists, nothing can run."
fi
say "Provider keys can be added later in $SECRETS_FILE; models are added in the app (Settings → Models → Add a model)."
