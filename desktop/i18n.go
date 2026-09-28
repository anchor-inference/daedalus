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
		"docker.missing":         "Daedalus needs Docker: install Docker Desktop, start it, and try again. Or set this up to run on this machine instead.",

		"watch.open":      "Open",
		"watch.away.one":  "%d notification while the launcher was away",
		"watch.away.few":  "%d notifications while the launcher was away",
		"watch.away.many": "%d notifications while the launcher was away",
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
		"docker.missing":         "Daedalus нужен Docker: установите Docker Desktop, запустите его и попробуйте снова. Или выберите запуск прямо на этом компьютере.",

		"watch.open":      "Открыть",
		"watch.away.one":  "%d уведомление, пока лаунчер был закрыт",
		"watch.away.few":  "%d уведомления, пока лаунчер был закрыт",
		"watch.away.many": "%d уведомлений, пока лаунчер был закрыт",
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
