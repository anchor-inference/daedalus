# Changelog

Notable changes, newest first. The repository's `main` is the released version.

## 2026-10-01

- **Daedalus installs and opens like any desktop application.** Windows has an installer
  (`Daedalus-Setup-x64.exe`: Next, Install, Finish; per user, no administrator; Start menu and desktop
  shortcuts; an entry in Apps & features whose uninstaller asks before it deletes any data), macOS a
  `.dmg` to drag into Applications, Linux a `.deb` with a menu entry and an AppImage. The window is the
  application's own (Electron), on all three systems: the launcher's setup and progress pages and the
  app are shown in it, never in the browser, and on Windows no console window appears behind it — the
  launcher now runs as the application's background process and writes to `launcher.log`.
- **The data lives in the per-user folder**, not beside the program: `%LOCALAPPDATA%\Daedalus\data`,
  `~/Library/Application Support/Daedalus/data`, `~/.local/share/daedalus/data`. An installation that
  kept `data/` beside an older launcher is moved there by its first start, once, by a rename that is
  refused while anything uses the folder; `daedalus-desktop import` brings one in from elsewhere.
- **A newer release is installed from the application's window** (*Install and restart*), through the
  launcher's own `upgrade`: the release's signature is checked, the data is protected first, and the
  data and the previous version come back if the new one does not start. The release archives are the
  unpacked application, so every older launcher upgrades into it the way it always upgraded.

## 2026-09-19

- **A session can pick its reasoning effort.** The composer has a chip that opens a slider
  upward, from **low** to **xhigh**; the choice stays on that session until the model is cleared
  back to the global default. Slash command `/thinking` accepts `xhigh` too.
- **Voice notes use the transcription endpoint.** A local speech model is the voice page's live
  listener; the composer microphone and a Telegram note follow Voice Notes in Settings. A browser
  recording is sent as WAV so the local model can still read it when no endpoint is set.
- **A local endpoint can pin sampling temperature.** llama.cpp, vLLM and a generic OpenAI-compatible
  client show the field; empty uses the host default.
- **llama.cpp is told the session's thinking settings.** Sending nothing left Bonsai at xhigh even
  when the preset asked for low. `high` is not a Bonsai effort and is sent as xhigh.

## 2026-09-18

- **The transcript opens without ending the voice conversation.** Pressing **Transcript** (or `T`)
  puts the conversation's own transcript where the orb was, and tapping an agent in the panel puts
  that agent's transcript there instead — with a breadcrumb saying which, its own composer so the
  operator can type to it, and the panel still beside it. Nothing is interrupted by any of that: the
  answer goes on being read out, the microphone stays open, a half-recognised utterance is not lost
  and the concierge's stream is not reopened. The microphone becomes a small floating control —
  bottom right on a wide window, bottom centre on a phone — that shows the same six states in the
  same six colours with a miniature of the orb's own motion, and barge-in and tap-to-talk work from
  it. **Back to voice** brings the orb back over a 200 ms cross-fade.
- **The voice session is no longer owned by the page that draws it.** The recogniser, the speaker
  and its queue, the event stream and the two engine-load watchers live in one object outside the
  component tree, held while the page is open and let go when it is left. That is what makes the
  paragraph above true rather than merely intended: changing what is on the screen builds nothing
  and tears nothing down. On a phone the transcript takes the whole screen and the agents panel
  becomes a sheet that rises over it.
- **A file can be found by name or by what is in it, without opening a folder at a time.**
  `GET /api/sessions/{id}/files/search?q=` searches the session's tree by name — a substring, or a
  glob when the query carries one — and `GET …/files/grep?q=` searches what is inside the files,
  literally rather than as a pattern, answering the line and the text on it. Both are bounded
  rather than complete, because a filter box types a letter at a time and a workspace with a
  `node_modules` in it is not a size anyone knows in advance: at most two hundred results, twenty
  thousand paths looked at, and a fraction of a second, whichever runs out first, with the answer
  saying when it was cut short. Both reach the disk through the containment the file browser
  already uses, so a symlink out of the tree, a `..` and anything belonging to the installation are
  absent from the answer rather than refused. Content search is ripgrep's work and is not imitated
  where ripgrep is missing: there it answers 501 and a sentence saying to search by name.
- **A message sent to a working agent can be taken back before it reads it.**
  `GET /api/sessions/{id}/steer` lists what is still waiting in front of the next model call, with
  the id and the moment each one was sent, and `DELETE /api/sessions/{id}/steer/{id}` withdraws
  one — 409 once the run has read it, because by then it is in the history and no longer in a
  queue. Withdrawal asks the running engine before it asks the store, which is the only order that
  works: a run holding the queue in memory would otherwise write its copy back over the removal.
  Whenever the queue changes the session's stream carries a `steer_changed` event with the whole
  queue in it, so a composer draws what is pending from the events instead of asking again.

- **The voice is built before it is needed, and an answer is never read out over the one before
  it.** The first answer of a session used to arrive on the screen and be read aloud about fifteen
  seconds later, and when the next answer came the page played both, one after the other. A voice
  that runs on this machine takes a second or two to become a synthesiser, and that second or two
  was being paid by the first thing the operator asked to hear — with the clips of that first answer
  still queued when the second one began. The voice is now loaded at the three moments it costs
  nothing: when the process starts with one configured, when it is chosen, and when the voice page
  is opened. `GET /api/voice` says where that load is and how long it took, `POST /api/tts/engine/warm`
  starts it explicitly, and `/api/tts/progress` carries it as it happens, so the page can say
  "loading the voice" rather than going quiet. Nothing unloads a voice that has gone silent: the
  operator about to say something else is the operator who just said something.
- **Every spoken sentence belongs to an answer, and only the newest answer is heard.** The sentences
  the server writes now name the run they came from, and the page plays only the run it is on: when
  a new answer starts — the operator speaks, interrupts, or the concierge begins writing again —
  what was queued for the old one is dropped, the request fetching the rest of it is aborted, and
  the sentences that were never said are marked on the screen as written rather than spoken.
- **An answer that arrives while the voice is still loading is read by the browser, for that answer
  only.** The next one, with the voice built, is read in the voice that was chosen. The page never
  waits in silence for a synthesiser, and it says which of the two is reading.
- **The voice page shows where the time went.** One quiet line under the answer: how long from the
  words being written to the first sound, how much of that was the voice being built, and how much
  was it speaking. In Russian and English, like the rest of the page.
- **Two things the microphone did wrong mid-answer.** Tapping it while an answer was being read put
  the page into "listening" although it was still speaking — so the chip was wrong for the whole
  answer and barging in, which only interrupts a page that is speaking, could not happen at all. And
  the tap primed the audio it was already playing, which paused the answer for six seconds, and
  cancelled the synthesiser's queue with the answer in it. Neither happens now, and a listener that
  keeps hearing words while the page speaks gets another chance to interrupt rather than one.
- **A better Russian voice, and a fourth kind of model behind it.** Settings → Voice offers
  **Supertonic 3**: thirty-one languages including Russian in one hundred and forty megabytes, ten
  voices inside it, forty-four kilohertz against the twenty-two of everything else here, and faster
  than any Piper voice on the same processor. It is now what a Russian installation is pointed at,
  with the Piper voices kept for anyone who would rather not take an OpenRAIL-M licence, which the
  card states rather than implies. **Supertonic 2** joins it as the English recommendation — twelve
  seconds of speech per second of work. An entry can now say it speaks several languages, and the
  picker's filter and its card both read that list instead of assuming one.
- **Russian stress is written in before the words are spoken.** «Приве́т», not «При́вет». Supertonic
  can be told where the stress falls, and a Russian answer going to it passes through a small
  dictionary of the words an assistant uses every day. A word the dictionary does not know, or one
  that is two words in writing — «замок» is a castle or a lock — is left exactly as it was, and no
  other voice is handed a mark it has no rule for.
- **The Kokoro entry pointed at the wrong build.** The hundred-and-fifty-megabyte quantised archive
  renders at nearly twice the cost of the three-hundred-and-fifty-megabyte float one it was supposed
  to accelerate, so the catalog now fetches the float build: the best-sounding English here stops
  being the one voice too slow to keep up with a person talking.
- **Which model actually answered is visible.** A run does not always finish on the model it was
  started with: when an endpoint refuses on quota or stops speaking, the provider chain steps down
  and the rest of the run is written by whatever is next on the list. Nothing anywhere said so — the
  header went on naming the configured model, the answer carried no mark, and the only trace was a
  row in the usage table nobody reads mid-conversation. Now every assistant turn records the model
  and provider that produced it, a run says on its event stream when the model answering changes and
  why, the session header reads "via <model> (fallback from <configured>)" for as long as the
  stand-in holds and goes back by itself when the configured model answers again, the turn a
  stand-in wrote carries a line above the answer that opens into the reason, and Telegram appends
  the same fact in one line under the delivered reply. The key proxy, which never reroutes a call
  but does rebuild the two subscription upstreams' replies around the requested model name, now
  names the upstream and the model it really sent in response headers, so a substitution there would
  be visible rather than invisible by construction.

## 2026-09-17

- **The optional pieces are listed, and installed from the app.** Settings → Components is every
  part an installation can do without — the speech runtime, downloaded recognition models and
  voices, the headless browser, Node, git, ripgrep, opus-tools, bubblewrap — with what each one
  unlocks, which skills are waiting for it, and how much it would cost to fetch. Each row is
  measured rather than assumed, so a portable build says what it is actually missing. What this
  process can install it installs, with a progress bar and one at a time; what the launcher owns it
  asks the launcher for; and what no download can fix says so instead of offering a button that can
  only fail. Where a component only takes effect after a restart, the page says so and offers one.
  The voice pages carry the same install button directly under the lines that explain that the
  browser is doing the listening and the speaking.
- **The moment after a run is drawn, and nothing is rewritten inside it.** A run that has ended
  still has a little to do behind the answer — the snapshot of the turn, the delivery to the other
  fronts — and the app said nothing about it at all: the session read as fully idle from the moment
  the answer landed. Now the bar stays up, quieter, saying the turn is being saved, and it goes when
  the saving is done. Undo, clear, fork and compaction wait for that moment to finish instead of
  running inside it, where an undo used to be overwritten by the snapshot it had just invalidated. A
  shutdown gives the same moment time to finish rather than cutting it off, and a restart asked for
  from Settings or from the agent's own apply button refuses while an agent is working, naming it.
- **The launcher says how an install ended.** Fetching Node or the headless browser is the
  launcher's job, and the only thing it used to say about itself was whether it was busy — which
  reads the same before the work starts and after it finishes. An install of something already
  cached, and one the launcher refused because it was doing something else, therefore both looked
  like an install still in progress: the Components page froze for half an hour and then reported a
  failure with no message in it. The launcher now claims the work before it answers, refuses what it
  cannot take, and reports the outcome of each action by name. There is a free-space check before a
  download starts, too.
- **Settings → Components is Russian on a Russian page.** The line on each card saying what was
  found — the largest text on it — was the server's English under translated headings. It is
  translated now, and hidden while an install is going, since it describes the very thing the
  install is changing.
- **A finished answer stops looking unfinished.** A run now announces that it is over before it
  tidies up after itself, and the app ends the turn on the model's own full stop rather than on the
  next poll. The cursor used to go on blinking under a completed answer for anything up to twenty
  seconds; it is gone within a frame of the last token, and the run chip within a frame of the
  host saying the run is done. The history, the workspace snapshot and everything a finished run
  hands on are unchanged — they simply no longer happen in front of you.
- **The voice concierge's model is chosen in the app.** Settings → Voice → Model is a list of the
  configured presets rather than a line of text you could only read: each one shows its label, its
  endpoint and its model id, the ones that think or that may write at length are marked as slow, and
  the first entry is the default model. The choice is written to `config.toml` and reaches the
  standing conversation at once — the next thing you say is answered by the model you just picked,
  with no restart and no new conversation. Where nothing configured answers quickly enough, both the
  card and the voice page say so and link to Add a model.
- **Install it without Docker.** The desktop launcher now has a second mode: instead of a container,
  it downloads a runtime into a folder it owns — a pinned `uv`, the CPython uv manages, `rg`, and the
  app's environment built from the checkout's own lock file — and starts `launcher/supervisor.py`
  under it as its own child. Every download is checked against the SHA-256 its publisher published
  before a byte of it is written, and a hash that does not match refuses the start by name.
  **Measured on Linux x86-64: 103 MB over the wire, 26 seconds from an empty folder to the app
  answering, 4.3 seconds warm**; 245 MB of runtime on disk, 390 MB for the whole installation with
  both checkouts. macOS is smaller and Windows larger by MinGit. The supervisor is unchanged and
  serves both modes — it never knew what a container was — and the first run's page asks which mode
  you want with what each costs written beside it.
- **The runtime image is 478 MB instead of 3.07 GB, and 114 MB to pull instead of 827.** It is built
  in stages now: the environment and the Mini App are made in builder stages and copied into a final
  image that carries neither a compiler, nor Node, nor the GitHub CLI, nor a second CPython, nor a
  browser. A container from it serves `/app/` at first start with no `uv sync` and no `npm` — the
  environment and the built app are baked. Browser skills move to the `:browser` tag (995 MB
  unpacked, 368 MB to pull, sharing every layer below the last), and without it they say they are not
  installed rather than writing scripts that cannot run. Both tags are `linux/amd64` **and**
  `linux/arm64`, so Apple Silicon pulls like everything else instead of building locally.
- **One image, two containers, three profiles.** The key proxy runs from the agent's own image with a
  different command — the isolation was always the container and never the image — which is 222 MB of
  download and one fewer image to build and sign. SearXNG (382 MB), the rebuilder (237 MB) and the
  local Bot API server (66 MB) are behind `--profile search`, `selfdev` and `telegram`, all off by
  default; `WebSearch` uses DuckDuckGo with no key and falls back to SearXNG when one is running. The
  launcher fetches both repositories as GitHub tarballs over `net/http` and commits them locally
  instead of pulling `alpine/git` (144 MB). **A desktop install went from 4.06 GB across five images
  to about 0.48 GB in one.**
- **An installation ships no model, and "no model yet" is a state rather than a crash.** A provider
  key is an address; which model runs on it — and what it costs — was never ours to guess, so the
  preset table is empty on a fresh install. The app opens on **Add a model** and stays there: the
  endpoint (the ones whose key the proxy holds are marked ready), the model from the list that
  endpoint serves with its context window, modalities and prices, and how it runs. Every other way in
  — the chat commands, the API (409), Telegram, `daedalus check`, `daedalus doctor` — answers one
  sentence naming the fix instead of a traceback. An existing `config.toml` is untouched.
- **An app window of its own.** The launcher opens the operating system's web view (WKWebView,
  WebView2) where there is one, a Chromium-family browser in application mode where there is not, and
  the default browser if neither — decided at run time, never a hard failure, and a Linux machine
  without WebKitGTK simply lands on the second step. The window remembers its size and position,
  `daedalus://open/<session-id>` opens a conversation from anywhere the desktop follows a link, a
  second launch focuses the first instead of starting another, and the inbox and a waiting question
  raise a real desktop notification. Binaries are 8 MB without the window and 10–12 MB with it.
- **`[self_change] mode`: `auto`, `off`, `local` or `server`.** What an installation may do to its own
  code is now one value, resolved once at startup from the prerequisites actually present — writable
  checkouts, a GitHub token, an `origin` on both, a way to deliver a build — and everything follows
  it: which `Self*` tools are registered at all, the extension, `/api/proposals`, the Changes screen,
  the self-development paragraphs of the prompt, the policy's push rule and the doctor's GitHub
  checks. `GET /api/capabilities` and `daedalus doctor` both name the mode and the reasons for it.
- **A desktop install improves itself locally.** In `local` mode the agent works in a worktree, runs
  the same relevance, evidence and size gates, and calls `SelfApply`, which fast-forwards its commits
  onto the checkout's own branch — no fork, no remote, no pull request. The app then shows *"Changes
  are ready — restart to apply"* with a **Restart** button, the launcher's status page shows the same,
  and closing the app and opening it again does it too, because the checkout is what runs. The restart
  is not a leap: the supervisor checks that commit out into a detached worktree of its own and runs
  `uv sync` (only when the lock or the project file changed), `compileall`, `daedalus check` and the
  smoke tests **in a virtualenv of its own**, so a refused change has touched nothing the running bot
  imports — and a change that passes but cannot stay up, three starts dying inside ten minutes, puts
  the last known-good commit back by itself.
- **Projects: a folder of yours is where an agent works, and the limit of every path it resolves.** A
  project is a row — name, root, settings — and a session points at one. `SessionServices.resolve` is
  the single point every file path passes through, so the containment could not be forgotten by a
  tool: the check is on the real path, which makes `..`, an absolute path elsewhere and a symlink out
  of the tree one refusal, and what comes back is the path that was judged. It bounds the file tools,
  the file browser, the preview, the download and `SendFile`; `Exec` runs in the folder and is bounded
  by the sandbox where one is on and by the policy rules where it is not. Every session a project
  session makes — a subagent, a spawned agent, a fork, a scheduled task, a delegate from the voice
  concierge — is in the same project, so the wall does not end at the first child. Several agents
  share a project and see the same files; a session without one still gets a scratch directory,
  exactly as before, and every session that already existed keeps it. Snapshots are off for a project
  by default — a project root is your repository, not a scratch directory — and the five directories
  a session writes into a folder go into `.git/info/exclude` where that folder is a git checkout. In
  Docker mode a project also needs a bind mount: the launcher asks the app which folders the container
  cannot see, writes them into the override, and says that a restart will mount them. Natively a
  project is reachable the moment it is added.
- **Voice mode (beta).** `/app/voice` is a conversation: a small fast model answers out loud in a
  second or two and hands anything substantial to real agent sessions while you keep talking, several
  at once, telling you when each comes back. It is an ordinary session with one flag — its transcript
  is in the app, its calls are in Usage — but the host, not a prompt, narrows it to five tools and
  keeps every other session away from them, and no mode can widen that.
- **Tool results stop growing the request.** `[tools.exec] max_output_chars` always bounded one call;
  it said nothing about the twenty results already in the transcript that every turn sent again.
  `[tools.results]` bounds those: the newest few are whole, older ones keep a head, and the trimming
  happens in batches so the prefix the provider caches is not invalidated on every turn. The stored
  history keeps every result intact — only the copy sent to the model is cut.
- **Native guardrails.** On your own machine the agent is a process of your own user, so two rules
  exist there that a container made unnecessary: the installation's own files — the provider keys, the
  state database, the secret that opens the restart channel, the launcher and the runtime it runs out
  of — are refused to read as well as to write, through `Exec` as much as through the file tools; and
  a path in your home folder outside every project, workspace, checkout and the installation itself is
  a question you answer once for one exact call. `Exec` is sandboxed by default on a native Linux
  machine where bubblewrap can actually run — `bwrap` installed and the kernel refusing it
  unprivileged namespaces, which is the Ubuntu 24.04 and Debian 13 default, starts with it off. Where the supervisor's command channel has to be a loopback port rather
  than a socket file, it asks for a secret it keeps beside the state. `daedalus doctor` and the
  launcher's status page say what the isolation is in one line rather than implying a wall that is
  not there.

## 2026-09-15

- **The desktop launcher is something you download and open.** The first release attached the bare
  binaries: a browser download carries no execute bit, so double-clicking `daedalus-desktop-darwin-arm64`
  opened the Mach-O in TextEdit, and Gatekeeper would have refused it in any case — the notes answered
  that with a `xattr` incantation, which is a workaround and not a product. Releases now carry archives:
  `Daedalus-macOS.zip` holding a `Daedalus.app` (one universal build for both kinds of Mac, its own icon,
  signed and notarized when the Apple secrets are set and ad-hoc when they are not, never unsigned and
  never a failed release for a missing secret), a tarball per Linux architecture that keeps the
  executable's mode, and a zip for Windows, with `SHA256SUMS` over all of them.
  `curl -fsSL .../desktop/install.sh | sh` installs either of the first two into `./Daedalus` and checks
  the download against those sums — and a file fetched with curl is never quarantined, so that path asks
  Gatekeeper nothing at all. Opened from Finder the launcher now behaves like an application rather than
  a command: its data folder is `data/` beside the `.app` — the folder you put the app in — instead of
  the root of the disk that a working directory of `/` would have made; Docker is looked for where the
  installers put it, because a program started from Finder inherits a PATH that names none of them; and a
  Docker that is missing or not started is shown on the launcher's own page, which opens first and stays
  up with the button to try again, instead of being printed to a terminal nobody has.

- **A fresh checkout builds its app before the first start.** The bundle is not in git and only a rebuild
  built it, so a new installation (the desktop launcher, a clone made by a setup script) answered 404 at
  `/app` until the first merged pull request. The supervisor builds it once when it is missing. Blank
  Telegram values in the env file (`OWNER_USER_ID=`) mean "unset" instead of failing to parse.
- **Every slash command the app offers, the app can run.** Twenty-two of the thirty-two commands the
  palette advertises were handed to the Telegram handlers and answered 409 on an installation with no
  bot token: `/stop`, `/model`, `/thinking`, `/mode`, `/rename`, `/new`, `/delete`, `/usage`, `/status`,
  `/sessions`, `/settings`, `/board`, `/schedules`, `/schedule`, `/intents`, `/peer`, `/heartbeat`,
  `/balance`, `/verbosity`, `/approval`, `/rebuild` and `/rollback` — the last two being the ones an
  operator without a chat most needs. They run on the session manager and the extensions directly now,
  and say the same as they do in the chat. `GET /api/commands` lists what this installation can actually
  run, so the palette never offers a command that answers with a refusal; the few whose subject *is* the
  chat — binding a forum, moving the private chat's window, closing a topic — say so rather than failing
  as unknown commands.
- **A session keeps its name when the chat gains topics.** A session opened while everything lived in the
  private chat had no topic of its own; binding a group left it talking in General with nothing saying
  which session it was, alongside every other one. `/bind` and the same switch in Settings → Chat open a
  topic for every session that has none, a session Telegram will not open one for yet gets it on its next
  output, and until then it speaks in General under its own name.
- **Smaller corrections.** `/use <n>` refuses a number `/sessions` never printed — both list thirty.
  Deleting a session through the API clears the private chat's pointer at it, as deleting one from the
  chat always did, instead of leaving the next message to open a session nobody asked for. The
  "Type your answer:" prompt carries the asking session's name, like the question card above it.

- **The desktop launcher's page answers only itself.** Its port is on the loopback address, but every
  page a browser has open can reach that address too, and a form post or a bodyless `fetch` travels
  cross-site without asking — enough for a page the operator merely visits to rewrite the provider
  keys and the Telegram identity the installation trusts, then restart the stack under them. The
  page now refuses anything that changes state and did not come from itself, and carries a token,
  minted per run, that the setup form and the buttons send back. Setup also stopped overwriting what
  it was not given: a field left empty keeps the value in force, with a *remove* box for emptying one
  on purpose, and the public address set by hand in `.env` — the host passkeys are enrolled against —
  survives a re-run. "Open" offers a pairing link once per start and only while it belongs to that
  start, mints a fresh one when there is none, and otherwise goes to the app's own login screen;
  `daedalus-desktop pair` prints a link whenever a browser needs one, and nothing reads links out of
  logs any more. A fork's checkout pulls the fork owner's images rather than upstream's, and `--port`
  refuses a number with something after it.
- **Sign-in hardened after review.** A pairing link is minted at start only when there is no other way in
  (no bot, no passkey) and the log names the file, never the link; a spent link lands on the app with the
  reason instead of an error page, and the file goes when the link is used. A passkey must be discoverable
  (the authenticator that refuses is told why), every ceremony has its own challenge so two browsers, or a
  stranger polling the login endpoint, cannot spend each other's, the cookie's `secure` flag follows the
  public address rather than a header any client can send, and Settings → Security can sign out everywhere.
  The prompt prefix now holds for loop agents too: the iteration counter and the next wake-up ride in the
  turn context. The supervisor's queued rebuild runs from a `finally`, never after an image rebuild.
- **The login page stands on its own.** It rendered inside the shell's grid on a wide screen, in the column
  kept for the rail, so it sat pinned to the left in a narrow strip. It is a centred card now, outside the
  shell, and offers every way in at once: a passkey when one is enrolled (and says how to get one when not),
  Telegram when a bot is configured, and a field for a pairing link or code.
- **Telegram is optional.** With no bot token the bot starts anyway: the API, the app, the scheduler, the
  loops, the inbox and self-development all run, and the front is simply absent. What used to be a chat line
  with nowhere to go — a failed rebuild, a resumed run, an exhausted budget, a low provider balance, a change
  proposal waiting for a decision, a fired reminder — becomes an inbox entry instead of a lost message; a
  session created without a chat is a session without a topic. `doctor` no longer fails an installation for
  having no bot, and reports what a browser can actually sign in with. The local Bot API server is behind the
  compose profile `telegram`, so the stack comes up without it.
- **Signing in without Telegram: a pairing link, then a passkey.** Every start prints a one-time link (in the
  log, and in `pairing-url` in the state directory, readable by its owner only); `daedalus auth pair` mints
  another. A link opens once, expires after thirty minutes, and using one revokes the rest. From the browser it
  signed in, Settings → Security enrols a **passkey** — a discoverable credential, so the login screen asks for
  no username — and the login screen then offers "Sign in with a passkey" beside the Telegram widget, or an
  explanation when neither is set up yet. The relying party follows `MINIAPP_PUBLIC_URL`, falling back to
  `localhost` for the app opened on the machine itself.
- **The session cookie is honest about the connection.** It is marked secure only when the browser really
  reached the app over TLS (directly or through the proxy that terminates it), so the http-on-localhost case
  can hold a session at all; and it is signed with the API token plus a secret minted once per installation
  rather than with the bot token, which an installation without Telegram does not have.
- **A desktop launcher.** `desktop/` builds one small binary per platform that turns a folder into a
  running Daedalus with Docker as the only thing installed on the host. It clones the bot and the
  core with git in a container, asks for a provider key, the optional Telegram values and a daily cap
  on a local page, writes the same `.env` and `daedalus-secrets/keyproxy.env` a server install uses,
  and runs `deploy/compose.yaml` unchanged over that folder. A generated override points the two
  services at the images published to GHCR and makes the Telegram containers optional, so a first
  start pulls instead of building and falls back to a local build when the platform has no image.
  `start`, `stop`, `status`, `logs`, `update`, `setup`, `open` and `uninstall` are also buttons on
  the launcher's page; closing the launcher leaves the stack running.
- **Telegram works without a group.** `telegram.mode` chooses where sessions live: `private` — every
  session in the operator's private chat, which is a window onto one of them at a time — or `topics`,
  today's forum with one topic per session. An installation that never bound a group is private, and
  `/bind` still switches it to topics, so nothing existing changes shape. In the private chat `/sessions`
  numbers the sessions, `/use <n|title>` points the chat at one, `/new <title>` creates one and writes to
  it, `/close` puts it away, and the chosen session survives a restart. Sessions the operator is not
  looking at keep talking: a scheduled report, a loop agent's answer or a question arrives with a line
  naming the session above it, an inline answer goes back to the session that asked, and so does a typed
  one — it is attributed by the message it replies to, not by whichever session the chat is on. Where the
  output of a session goes is decided in one place (`outbox_for_session`), so both shapes share the whole
  feature set: files, voice, live status per run, approval cards, notifications and the Mini App.
- **The prompt prefix is stable across runs.** The clock and the workspace notes (`AGENTS.md`, the open board
  tasks) were rendered into the system prompt at every run, so the first request of every run began with a
  different first message and the provider re-read the whole history behind it: measured at run starts, the
  cache hit was near zero. Both now ride in a `<turn-context>` block at the end of the run's opening message;
  the system prompt is byte-identical from one run of a session to the next. The app, the summariser, the
  compaction quotes and the learning digest strip the block.
- **A rebuild asked for during another one is queued.** The supervisor used to answer "already in progress" and
  forget the request, so a pull request merged during a rebuild waited for the next one. One slot: the latest
  reason wins (the target is origin/main either way), it starts when the lock is free, and a rollback drops it.
- **The agent is told what is private to the machine.** The environment section and the self-development skill
  name what the proposal gate refuses (the LAN address of its services, hostnames, the operator's paths and
  accounts), so a proposal is not refused for a value the agent read from ServiceList. StaySilent's refusal in
  an attended run says what to do instead.

## 2026-09-14

- **Exec refuses to wait.** A `sleep` of 30 s or more, or a `while`/`until` loop around one, in a foreground
  Exec is denied with the reason: a subagent's report and a finished job arrive as messages that wake the run
  the moment they are ready, and a sleep only holds the run that would read them. The agent is told to end
  the turn when nothing else needs doing, or to read a job with JobOutput. Short sleeps, background jobs and
  services that sleep in a loop are unaffected; the prompt says the same next to SubAgent.

## 2026-09-13

- **Every overlay is rendered in the document, not where it was declared.** The Access sheet of a service
  (like its menu before it) sat inside a pressable card: while the mouse button was down the card's press
  transform made it the containing block of the fixed sheet, the sheet snapped onto the card with its body
  clipped, and the release landed outside "Local only". Sheets, the file preview, the confirmation dialog and
  the command-result and model-picker panels now go through one portal that also keeps their clicks from
  reaching the row underneath; a browser check drives the real mouse over the Access sheet.
- **A compaction is visible while it runs.** The session shows "Compacting" with a progress bar in the chat
  (how many messages, which part of the transcript is being summarised, merging, writing) and in the agents
  list, whether it was started from the Mini App or with `/compact` in Telegram; the stream carries the
  progress, so nothing has to be guessed at.
- **The session layout is remembered.** The pane opened on the right (files, MCP servers) and whether the
  session panel is shown stay as set across sessions and reloads.
- **Agents list: a shared directory groups its owner too.** The session a directory was made for sits in the
  group with the agents that joined it, and the group is named after that session.
- **Every agent has its own board.** `BoardList`, `BoardGet` and `BoardUpdate` see the tasks created by the
  session and its subagents, plus the tasks the operator posted to nobody in particular; another agent's tasks
  are not on it, so a loop agent reading "the board" reads its own plan and cannot pick up a colleague's bug
  fix. The work-in-progress limit counts what one agent holds, not the whole installation. The operator's board
  in the Mini App still shows everything, says whose board each task is on, and a new task can be addressed to
  one agent (`session_id` on `POST /api/board`) or left for whoever takes it.
- **A provider outage no longer ends the task.** The core retries a failed stream in place for seconds; when
  the provider stays unreachable the run failed and stayed failed until the operator wrote something. The host
  now drives the turn again after a wait that doubles per consecutive failure (`ops.provider_retry_*`, 30 s to
  10 min, six attempts), unless the session moved on meanwhile.

## 2026-09-11

- **The app is an application.** Every screen is an address under `/app` (`/app/agents/<id>` is a session,
  `/app/settings/models` a settings section), so Back, a reload, Telegram's back button and a link all land on
  the same screen; the server answers the app for any of its paths. A phone shows four tabs (Agents, Inbox,
  Board, More) with the rest behind More; a desktop has a rail with grouped destinations that folds to icons, a
  search-and-go palette on Ctrl/⌘ K, and `g` + a letter to jump between screens. Each screen names itself and
  keeps its actions in its header; the brand line is gone.
- **Rows instead of cards.** Agents are grouped by what they need (Needs you, Working, Loops, Idle) and carry the
  model, the time and the loop line, with subagents inside the leader's card and no raw ids. Inbox entries have a
  kind icon and severity, repeats fold into one card with a count, and a pending change is a card with Approve
  and Reject on it. The board is a kanban on a wide screen (cards drag between columns) and sections on a phone;
  a task shows its priority as a stripe and its checklist as a bar. A schedule reads as a sentence in the reader's
  own clock and is made from a moment, a daily or weekly time or a cadence; cron stays for the rest. Services
  open with one button and say who can reach them in plain words. Memories are clamped cards with bulk selection
  from the header. Usage leads with what is metered today, the tightest quota and the tokens, and every quota
  line says what is left and when it resets. Settings is an index of sections, side by side on a wide screen.
- **A session in less chrome.** The header is back, title and one status line (model, context when it matters,
  subagents, loop) with an overflow menu; tokens, cost, ids and the settings live in the Session info sheet. A
  live bar over the composer says which step the agent is on; the composer is one line, Stop asks first, and
  revert and fork sit in a menu on the message. On wide screens the sessions list can stay beside the
  conversation and the inspector sits on the right.
- **Shared behaviour.** Confirmations say what the action does and focus the safe button; deletions can be
  undone for five seconds; Escape closes only the top layer; times, tokens, money and schedules format through
  one module in the reader's locale, never with seconds or an ISO timestamp; `idle` is grey, so working and
  waiting stand out. A schedule can be edited and paused (`PATCH /api/schedules/{id}`), and the roster carries
  each session's model.
- **Removed models stay removed.** The Claude subscription seed re-added its presets on every start; a seed is
  now applied once and recorded in the config (`seeded`).
- **Settings, models and clients.** One line per model and per client — label, client · model, and pills for
  default, fallback, thinking, images and window — that opens into an editing panel; the same rows stack on a
  phone and spread on a desktop.
- **Rebuild without the outage.** The supervisor preflights a merged revision on a candidate checkout while
  the bot keeps serving, and stops it only to sync dependencies and restart; a revision that fails the
  preflight never touches the running bot. Sandbox wrappers the bot leaves behind are reaped.
- **Services run inside the sandbox.** `ServiceStart` ran its command outside the wall `Exec` has, and the agent
  used it to write where `Exec` could not. A service now runs under the same sandbox; the worktrees a session
  opens with `SelfWorkspace` are writable for that session's commands, which is where those writes belonged.
  A service can be removed from the app (stopped first if it runs); subagents are no longer listed twice in a
  session's sidebar.
- **Fallback chain.** A rung whose context window cannot hold the current prompt plus its output cap is
  skipped with that reason; when nothing fits, the loop retries where it was instead of ending the run on a
  model that could not take it. A subagent that ends without a reply names the error that ended it.
- **Compaction.** The whole-history pass also runs before a run that would start over the ratio (a session
  switched to a smaller-window model); the summariser's cap fits a summary in any script, a unit the
  summariser keeps failing on is left alone, and a compaction pass no longer renames the topic there and back.
- **OpenCode Go.** A provider kind `opencode` for the OpenCode Go / Zen gateway: the key proxy knows the upstream
  (`OPENCODE_API_KEY`), every request carries the session id the gateway routes and caches by, thinking goes out
  in DeepSeek's shape, and presets for its models carry the gateway's list prices so the metered spend tracks the
  subscription's allowance. Prices for the gateway's models are refreshed daily from models.dev (the operator's own
  entries win), and the Usage screen shows the subscription's 5-hour, weekly and monthly allowance windows next
  to the Codex, Grok and Claude ones.

## 2026-09-10

- **Reasoning that outruns the output cap.** The core no longer keeps a reasoning block the cap cut, and no
  longer asks the model to resume it: the retry sends the same prompt with the effort lowered, then with thinking
  off, at most twice, and the knobs return afterwards. DeepSeek presets send the effort names DeepSeek knows
  (`medium` is its default `high`). Size a DeepSeek preset's `max_output_tokens` at the vendor's thinking-mode
  default, 64K: at 32K the model ran out of budget while still reasoning on hard tasks.
- **Bench.** Memory tools are off in benchmark sessions; an Exec that hits its timeout says how to run the
  command in the background; a task that declares GPUs is excluded with `-x` rather than aborting the job.
- **Self-development gates.** `SelfPropose` takes an `execution_path`; a host-module change is refused when nothing
  reaches it, when no passing Verify receipt names the changed code, or when a large change does not say what it
  replaces. The preflight asserts every module is reachable from the entry points.
- **Tool policy.** Built-in rules refuse commands that act on the machine, the operator's paths or checkouts; a
  forced push, deleting a workspace or a host outside the egress allowlist need the operator's approval key
  (`/allow <key>`, the app, or the API). Operator rules (`[policy]`) tighten, never loosen. Operator scripts
  (`[hooks]`) run before and after tools and after runs.
- **Bench.** `daedalus bench <manifest>` runs recorded tasks headless and records verdict, turns, tokens, cost and a
  trajectory per task; `daedalus.bench.harbor:DaedalusAgent` runs the same loop under Harbor against benchmark
  containers. A provider endpoint can pin its sampling temperature.
- **Tools.** `Exec(background=true)` with `JobOutput`/`JobKill`/`JobList`; long output keeps head and tail and is
  spilled to a file; `Edit` matches loosely and shows the closest lines on a miss; `MultiEdit`; a written Python
  file is parsed and linted in the result; `SubAgent` takes `expects`, `deliverable` and `persona`; `Skill` fills
  a recipe's parameters; `SkillDraft` saves a skill distilled from a session.
- **Modes and budgets.** A `plan` mode allows only read-only tools until the operator switches it; runs can be
  capped by active minutes and by tokens; the compaction summariser and memory extraction can run on a cheaper
  preset; `AGENTS.md` and the session's open board tasks ride along in the prompt; the board writes `PLAN.md`.
- **Ops.** The sandbox fails closed; every tool call leaves a timing row and every network host an egress row
  (shown in the app); a session exports as one Markdown file; `deploy/setup.sh` sets a new installation up.
- **Memory.** Recall ranks with BM25 and recency; optional extraction of durable facts after a run.
