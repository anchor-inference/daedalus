package main

// Every line of every launcher page, in both languages it speaks.
//
// The tables are the whole translation layer: templates ask for a key and never hold a sentence,
// and the script that drives the pages is handed the same table as JSON so a line rendered by Go
// and a line written by the browser come from one place. A key that exists in one language and not
// the other is a page that would come out half translated, which is why i18n_test.go compares the
// two tables both ways rather than trusting that a pair was added together.

import (
	"encoding/json"
	"sort"
)

// messages is the table. Keys are grouped by the page they belong to; `step.` and the handful of
// words the status tiles share are on more than one page.
var messages = map[Lang]map[string]string{
	LangEN: {
		"setup.title": "Set Daedalus up",
		"setup.lead":  "Three answers and it runs.",
		"setup.where": "Written on this machine only, to",

		"setup.step.mode":  "How it runs",
		"setup.step.key":   "A model to think with",
		"setup.step.money": "Spending",

		"setup.mode.native":      "On this machine",
		"setup.mode.native.line": "Light and quick. Nothing else to install.",
		"setup.mode.suggested":   "Works without Docker",
		"setup.mode.docker":      "In a container",
		"setup.mode.docker.line": "Walled off from your files. Needs Docker.",
		"setup.mode.more":        "Which should I pick?",
		"setup.mode.more.body":   "A container is a wall around the agent: a command that goes wrong stops at it. Without one the agent runs as you — the approval gates, the path rules and the spending cap all still hold, but the wall is gone. Docker Desktop is an application of its own and about half a gigabyte of images; on this machine, the choice already selected is the one that works today.",

		"setup.key.lead":   "One key is enough.",
		"setup.key.field":  "API key",
		"setup.key.remove": "Remove this key",
		"setup.key.skip":   "Skip — add one later in the app",
		"setup.key.note":   "Keys stay on this machine and reach the provider through a proxy the agent cannot read. Signed in to the Codex, Claude Code or SuperGrok CLI here? That counts as a key.",

		"setup.cap.field": "A day's spending, USD",
		"setup.cap.note":  "Enforced twice: by the supervisor the agent cannot edit, and by the proxy that stops paying.",

		"setup.telegram.name":     "Telegram",
		"setup.telegram.optional": "optional",
		"setup.telegram.lead":     "Add it to write to the agent from your phone. Without it you use the app in a window.",
		"setup.telegram.token":    "Bot token",
		"setup.telegram.owner":    "Your numeric user id",
		"setup.telegram.apiid":    "Telegram API id",
		"setup.telegram.apihash":  "Telegram API hash",

		"setup.remove":      "Remove",
		"setup.submit":      "Save and start",
		"setup.submit.note": "The first start downloads what it needs, then opens the app.",

		"progress.title":         "Getting everything ready",
		"progress.lead":          "The first time takes a few minutes. You can leave this open.",
		"progress.working":       "Working…",
		"progress.idle":          "Not running — press Start to bring it up",
		"progress.done":          "Ready — opening the app",
		"progress.error.title":   "That did not work",
		"progress.error.retry":   "Try again",
		"progress.error.setup":   "Change the configuration",
		"progress.error.details": "What happened",
		"progress.error.raw":     "Something went wrong that the launcher does not recognise. The program said:",

		"step.runtime":     "Downloading the runtime",
		"step.images":      "Fetching the images",
		"step.checkouts":   "Getting the code",
		"step.environment": "Building the environment",
		"step.start":       "Starting",

		// The one line that moves while a start runs. It says what the launcher is at in the
		// operator's language; the machine's own commentary stays under it, where it reads as the
		// log it is.
		"live.runtime":     "Downloading the tools it runs on. This happens once.",
		"live.images":      "Fetching the container images — the long part of a first run.",
		"live.checkouts":   "Getting the agent's own code.",
		"live.environment": "Building the environment. The longest step, and only the first time.",
		"live.start":       "Bringing everything up.",

		// The failures that actually happen on a first run, answered rather than reported. What
		// the program itself said is kept on the page, behind "What happened".
		"trouble.network": "This machine could not reach the internet. Check the connection — a VPN or a company proxy is the usual reason — and try again.",
		"trouble.docker":  "Docker is not answering. Start Docker Desktop, wait until it says it is running, and try again. Or set this up to run on this machine instead.",
		"trouble.port":    "A port Daedalus needs is taken by something else on this machine. Close whatever is using it, or set a different port in the configuration, and try again.",
		"trouble.disk":    "This machine has run out of disk space. Free some up and try again — a first start needs a couple of gigabytes.",

		"status.title":           "Daedalus",
		"status.mode.native":     "runs on this machine",
		"status.mode.docker":     "runs in a container",
		"status.tile.docker":     "Docker",
		"status.tile.runs":       "Runs on",
		"status.tile.machine":    "this machine",
		"status.tile.containers": "Containers",
		"status.tile.processes":  "Processes",
		"status.tile.telegram":   "Telegram",
		"status.tile.app":        "App",
		"status.tile.ports":      "Ports",
		"status.open":            "Open the app",
		"status.open.idle":       "Nothing is running yet — start it first",
		"status.start":           "Start",
		"status.stop":            "Stop",
		"status.update":          "Update",
		"busy.start":             "Starting…",
		"busy.stop":              "Stopping…",
		"busy.apply":             "Applying the change…",
		"busy.restart":           "Restarting…",
		"busy.update":            "Updating…",
		"busy.uninstall":         "Removing…",
		"status.configure":       "Change the configuration",
		"status.log":             "What the launcher is doing",
		"status.log.empty":       "nothing yet",
		"status.on":              "on",
		"status.off":             "off — the app only",
		"status.none":            "none yet",
		"status.unavailable":     "not available",
		"status.running":         "running",
		"status.close.native":    "Closing this stops the agent. A run in flight is saved and picks up at the next start.",
		"status.close.docker":    "Closing this leaves everything running in the background.",
		"status.native.note":     "No container here: the agent's commands run as you. It asks before anything it touches leaves the project, and refuses its own files outright.",
		"status.logs.files":      "The agent's own logs are files under",
		"status.logs.command":    "The stack's own logs:",
		"status.silent":          "The launcher is not answering. It may have been closed; what it started keeps running.",
		"change.pending":         "A change is ready — restart to apply it",
		"change.apply":           "Restart to apply",
		"change.applied":         "The change is running",
		"change.reverted":        "The change was reversed",
		"change.failed":          "The change was not applied",
		"alert.docker":           "Docker is not available",
		"alert.failure":          "The last action did not finish",
		"alert.silent":           "Lost touch with the launcher",
		"docker.missing":         "Daedalus needs Docker: install Docker Desktop, start it, and try again. Or set this up to run on this machine instead.",

		"watch.open":      "Open",
		"watch.away.one":  "%d notification while the launcher was away",
		"watch.away.few":  "%d notifications while the launcher was away",
		"watch.away.many": "%d notifications while the launcher was away",

		"upgrade.notify.title":      "Daedalus %s is available",
		"upgrade.notify.body":       "You have %s. Nothing is installed by itself: close the launcher and run `daedalus-desktop upgrade` — it keeps your data as it is now before it changes anything.",
		"upgrade.card.title":        "Daedalus %s is available",
		"upgrade.card.body":         "This launcher is %s. Nothing is installed by itself: close the launcher and run the command below in a terminal. It asks first, keeps your data as it is now — a whole copy on Linux with ext4, a checked backup elsewhere — and puts the data and this launcher back if the new version does not come up.",
		"upgrade.card.body.shell":   "This is %s. Nothing is installed by itself. Install it from here: Daedalus closes, keeps your data as it is now — a whole copy on Linux with ext4, a checked backup elsewhere — puts the new version in place and opens again. If the new version does not come up, your data and this version are put back.",
		"upgrade.card.body.package": "This is %s, installed by a package or as an AppImage, which cannot replace itself. Download the new version's file from the release page and install it the way you installed this one; your data stays where it is.",
		"upgrade.card.install":      "Install and restart",
		"upgrade.card.release":      "Open the release page",
		"upgrade.notify.body.shell": "You have %s. Open Daedalus to install it.",

		"switch.card.title":      "Kept copies of your data",
		"switch.card.trouble":    "Something here needs you",
		"switch.card.command":    "daedalus-desktop update status",
		"switch.item.retained":   "Kept: %s",
		"switch.item.unrecorded": "Kept without a record of its contents — remove it by hand once you no longer need it: %s",
		"switch.item.late":       "A late write may be in %s",
		"switch.item.lost":       "A write may have been lost while this copy was being removed: %s",
		"switch.item.unfinished": "A switch of the data folder did not finish (%s). Nothing was deleted; run daedalus-desktop update resolve to see what is where",
		"switch.item.upgrade":    "The last update did not finish (%s). Nothing starts until it is undone: run daedalus-desktop upgrade --rollback",
		"switch.item.trash":      "A copy is in the trash of the updates' folder: %s",
		"switch.item.stray":      "A copy an update left beside the data folder: %s. daedalus-desktop update resolve says whose it is",

		"switch.item.unscanned.one":  "%s process could not be inspected during the last switch; `update status -v` names it",
		"switch.item.unscanned.few":  "%s processes could not be inspected during the last switch; `update status -v` names them",
		"switch.item.unscanned.many": "%s processes could not be inspected during the last switch; `update status -v` names them",
		"switch.remove":              "Remove",
		"switch.remove.confirm":      "Remove the kept copy %s? It cannot be undone.",
		"switch.size.kb":             "%s KB",
		"switch.size.mb":             "%s MB",
		"switch.size.gb":             "%s GB",
		"trouble.fence.refused":      "The update did not start: a program is still writing into the data folder. Nothing was changed. Close whatever uses it — an editor, a terminal inside it — and press Update again.",
		"trouble.fence.rolledback":   "The update was undone at once: a program wrote into the data folder while it was being switched. The data is exactly as it was. Close whatever uses it and press Update again.",
		"trouble.fence.failclosed":   "The update did not start: the data folder could not be switched safely on this machine. Nothing was changed. `daedalus-desktop update status` in a terminal shows the report.",
		"trouble.fence.unfinished":   "The update's switch of the data folder did not finish, and nothing starts until it is settled. The data is not lost: `daedalus-desktop update resolve` in a terminal says what is where, and with --apply settles it.",
	},
	LangRU: {
		"setup.title": "Настройка Daedalus",
		"setup.lead":  "Три ответа — и можно работать.",
		"setup.where": "Сохраняется только на этом компьютере, в",

		"setup.step.mode":  "Как запускать",
		"setup.step.key":   "Модель, которой он думает",
		"setup.step.money": "Расходы",

		"setup.mode.native":      "На этом компьютере",
		"setup.mode.native.line": "Легко и быстро. Ставить больше нечего.",
		"setup.mode.suggested":   "Работает без Docker",
		"setup.mode.docker":      "В контейнере",
		"setup.mode.docker.line": "Отдельно от ваших файлов. Нужен Docker.",
		"setup.mode.more":        "Что выбрать?",
		"setup.mode.more.body":   "Контейнер — это стена вокруг агента: неудачная команда останавливается на ней. Без контейнера агент работает от вашего имени — подтверждения, правила путей и лимит расходов остаются, но стены нет. Docker Desktop — отдельное приложение и примерно полгигабайта образов; отмечен тот вариант, который на этом компьютере работает уже сейчас.",

		"setup.key.lead":   "Хватит одного ключа.",
		"setup.key.field":  "API-ключ",
		"setup.key.remove": "Удалить этот ключ",
		"setup.key.skip":   "Пропустить — добавлю позже в приложении",
		"setup.key.note":   "Ключи остаются на этом компьютере и уходят к провайдеру через прокси, который агент не может прочитать. Если здесь выполнен вход в Codex, Claude Code или SuperGrok CLI — это тоже ключ.",

		"setup.cap.field": "Расход в день, USD",
		"setup.cap.note":  "Лимит держат двое: супервизор, который агент не может изменить, и прокси, который перестаёт платить.",

		"setup.telegram.name":     "Telegram",
		"setup.telegram.optional": "необязательно",
		"setup.telegram.lead":     "Нужен, чтобы писать агенту с телефона. Без него всё то же самое в окне приложения.",
		"setup.telegram.token":    "Токен бота",
		"setup.telegram.owner":    "Ваш числовой id",
		"setup.telegram.apiid":    "Telegram API id",
		"setup.telegram.apihash":  "Telegram API hash",

		"setup.remove":      "Удалить",
		"setup.submit":      "Сохранить и запустить",
		"setup.submit.note": "При первом запуске загрузится всё нужное, потом откроется приложение.",

		"progress.title":         "Готовим всё к работе",
		"progress.lead":          "Первый раз занимает несколько минут. Окно можно не закрывать.",
		"progress.working":       "Работаем…",
		"progress.idle":          "Не запущено — нажмите «Запустить»",
		"progress.done":          "Готово — открываем приложение",
		"progress.error.title":   "Не получилось",
		"progress.error.retry":   "Попробовать снова",
		"progress.error.setup":   "Изменить настройки",
		"progress.error.details": "Что случилось",
		"progress.error.raw":     "Что-то пошло не так, и лаунчер не знает, что именно. Программа сообщила:",

		"step.runtime":     "Загрузка среды",
		"step.images":      "Загрузка образов",
		"step.checkouts":   "Загрузка кода",
		"step.environment": "Сборка окружения",
		"step.start":       "Запуск",

		"live.runtime":     "Загружаем то, на чём он работает. Это бывает один раз.",
		"live.images":      "Загружаем образы контейнеров — самая долгая часть первого запуска.",
		"live.checkouts":   "Загружаем код самого агента.",
		"live.environment": "Собираем окружение. Самый долгий шаг, и только в первый раз.",
		"live.start":       "Поднимаем всё остальное.",

		"trouble.network": "С этого компьютера не получилось выйти в интернет. Проверьте соединение — чаще всего мешает VPN или корпоративный прокси — и попробуйте снова.",
		"trouble.docker":  "Docker не отвечает. Запустите Docker Desktop, дождитесь, пока он скажет, что работает, и попробуйте снова. Или выберите запуск прямо на этом компьютере.",
		"trouble.port":    "Порт, который нужен Daedalus, занят другой программой. Закройте её или укажите другой порт в настройках и попробуйте снова.",
		"trouble.disk":    "На диске закончилось место. Освободите его и попробуйте снова — первому запуску нужна пара гигабайт.",

		"status.title":           "Daedalus",
		"status.mode.native":     "работает на этом компьютере",
		"status.mode.docker":     "работает в контейнере",
		"status.tile.docker":     "Docker",
		"status.tile.runs":       "Работает на",
		"status.tile.machine":    "этом компьютере",
		"status.tile.containers": "Контейнеры",
		"status.tile.processes":  "Процессы",
		"status.tile.telegram":   "Telegram",
		"status.tile.app":        "Приложение",
		"status.tile.ports":      "Порты",
		"status.open":            "Открыть приложение",
		"status.open.idle":       "Пока ничего не запущено — сначала запустите",
		"status.start":           "Запустить",
		"status.stop":            "Остановить",
		"status.update":          "Обновить",
		"busy.start":             "Запускается…",
		"busy.stop":              "Останавливается…",
		"busy.apply":             "Применяется изменение…",
		"busy.restart":           "Перезапускается…",
		"busy.update":            "Обновляется…",
		"busy.uninstall":         "Удаляется…",
		"status.configure":       "Изменить настройки",
		"status.log":             "Что делает лаунчер",
		"status.log.empty":       "пока ничего",
		"status.on":              "включён",
		"status.off":             "выключен — только приложение",
		"status.none":            "пока нет",
		"status.unavailable":     "недоступен",
		"status.running":         "запущено",
		"status.close.native":    "Если закрыть, агент остановится. Начатое сохранится и продолжится при следующем запуске.",
		"status.close.docker":    "Если закрыть, всё продолжит работать в фоне.",
		"status.native.note":     "Контейнера здесь нет: команды агента выполняются от вашего имени. Он спрашивает, прежде чем выйти за пределы проекта, и не трогает собственные файлы установки.",
		"status.logs.files":      "Логи самого агента — файлы в",
		"status.logs.command":    "Логи всего стека:",
		"status.silent":          "Лаунчер не отвечает. Возможно, он закрыт; то, что он запустил, продолжает работать.",
		"change.pending":         "Есть изменение — перезапустите, чтобы применить",
		"change.apply":           "Перезапустить и применить",
		"change.applied":         "Изменение работает",
		"change.reverted":        "Изменение откачено",
		"change.failed":          "Изменение не применилось",
		"alert.docker":           "Docker недоступен",
		"alert.failure":          "Последнее действие не завершилось",
		"alert.silent":           "Связь с лаунчером потеряна",
		"docker.missing":         "Daedalus нужен Docker: установите Docker Desktop, запустите его и попробуйте снова. Или выберите запуск прямо на этом компьютере.",

		"watch.open":      "Открыть",
		"watch.away.one":  "%d уведомление, пока лаунчер был закрыт",
		"watch.away.few":  "%d уведомления, пока лаунчер был закрыт",
		"watch.away.many": "%d уведомлений, пока лаунчер был закрыт",

		"upgrade.notify.title":      "Вышел Daedalus %s",
		"upgrade.notify.body":       "У вас %s. Само ничего не ставится: закройте лаунчер и выполните `daedalus-desktop upgrade` — он сначала сохранит данные такими, какие они сейчас.",
		"upgrade.card.title":        "Вышел Daedalus %s",
		"upgrade.card.body":         "Этот лаунчер — %s. Само ничего не ставится: закройте лаунчер и выполните команду ниже в терминале. Она спросит подтверждение, сохранит данные такими, какие они сейчас — целой копией на Linux с ext4, проверенным бэкапом в остальных случаях, — а если новая версия не поднимется, вернёт данные и этот лаунчер как было.",
		"upgrade.card.body.shell":   "У вас %s. Само ничего не ставится. Установите отсюда: Daedalus закроется, сохранит данные такими, какие они сейчас — целой копией на Linux с ext4, проверенным бэкапом в остальных случаях, — поставит новую версию и откроется снова. Если новая версия не поднимется, данные и эта версия вернутся как было.",
		"upgrade.card.body.package": "У вас %s, установленная пакетом или как AppImage, — она не может заменить себя сама. Скачайте файл новой версии со страницы релиза и установите так же, как эту; данные останутся на месте.",
		"upgrade.card.install":      "Установить и перезапустить",
		"upgrade.card.release":      "Открыть страницу релиза",
		"upgrade.notify.body.shell": "У вас %s. Откройте Daedalus, чтобы установить.",

		"switch.card.title":      "Сохранённые копии ваших данных",
		"switch.card.trouble":    "Здесь нужно ваше внимание",
		"switch.card.command":    "daedalus-desktop update status",
		"switch.item.retained":   "Сохранено: %s",
		"switch.item.unrecorded": "Сохранено без описи содержимого — удалите вручную, когда станет не нужно: %s",
		"switch.item.late":       "Возможно, в %s попала поздняя запись",
		"switch.item.lost":       "Пока удалялась эта копия, запись могла потеряться: %s",
		"switch.item.unfinished": "Переключение папки данных не завершилось (%s). Ничего не удалено; выполните daedalus-desktop update resolve, чтобы увидеть, что где",
		"switch.item.upgrade":    "Последнее обновление не завершилось (%s). Пока его не отменить, ничего не запустится: выполните daedalus-desktop upgrade --rollback",
		"switch.item.trash":      "Копия лежит в корзине папки обновлений: %s",
		"switch.item.stray":      "Копия, которую обновление оставило рядом с папкой данных: %s. daedalus-desktop update resolve скажет, чья она",

		"switch.item.unscanned.one":  "%s процесс не удалось проверить во время последнего переключения; `update status -v` назовёт его",
		"switch.item.unscanned.few":  "%s процесса не удалось проверить во время последнего переключения; `update status -v` назовёт их",
		"switch.item.unscanned.many": "%s процессов не удалось проверить во время последнего переключения; `update status -v` назовёт их",
		"switch.remove":              "Удалить",
		"switch.remove.confirm":      "Удалить сохранённую копию %s? Это нельзя отменить.",
		"switch.size.kb":             "%s КБ",
		"switch.size.mb":             "%s МБ",
		"switch.size.gb":             "%s ГБ",
		"trouble.fence.refused":      "Обновление не началось: какая-то программа всё ещё пишет в папку данных. Ничего не изменено. Закройте то, что её использует, — редактор, терминал в ней — и нажмите «Обновить» ещё раз.",
		"trouble.fence.rolledback":   "Обновление сразу отменено: пока папка данных переключалась, в неё что-то записало. Данные в точности такие, какими были. Закройте то, что её использует, и нажмите «Обновить» ещё раз.",
		"trouble.fence.failclosed":   "Обновление не началось: на этом компьютере папку данных не удалось безопасно переключить. Ничего не изменено. Отчёт покажет `daedalus-desktop update status` в терминале.",
		"trouble.fence.unfinished":   "Переключение папки данных при обновлении не завершилось, и пока это не улажено, ничего не запустится. Данные не потеряны: `daedalus-desktop update resolve` в терминале скажет, что где, а с --apply уладит.",
	},
}

// Translate is the one way a line is looked up. A key with no line anywhere comes back as itself,
// which is a visible defect on the page rather than an empty space — and cannot happen in a build
// whose tests pass.
func Translate(lang Lang, key string) string {
	if line, ok := messages[lang][key]; ok {
		return line
	}
	if line, ok := messages[LangEN][key]; ok {
		return line
	}
	return key
}

// MessagesJSON is the same table for the script that drives the pages, so a line the browser writes
// is the line Go would have written. It is rendered into the page rather than fetched: the page has
// to read correctly before anything answers.
func MessagesJSON(lang Lang) string {
	table := messages[lang]
	if table == nil {
		table = messages[LangEN]
	}
	body, err := json.Marshal(table)
	if err != nil {
		return "{}"
	}
	return string(body)
}

// messageKeys is what the test compares. Sorted, so a failure names the first missing key rather
// than whichever one the map handed over first.
func messageKeys(lang Lang) []string {
	keys := make([]string, 0, len(messages[lang]))
	for key := range messages[lang] {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
