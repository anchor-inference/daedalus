package markdown

import (
	"regexp"
	"strings"
)

// The rich-answer syntax the app draws as cards, flows and chips. A terminal draws none of it, so
// Flatten turns it into Markdown a terminal reads, the way the Telegram front does
// (daedalus/transport/telegram/render.py, flatten_rich and flatten_evidence); the two must agree, and
// the tests mirror the host's.
var (
	evidenceRE  = regexp.MustCompile(`<\s*(file|run)\s+([^<>]*?)/?\s*>`)
	attrRE      = regexp.MustCompile(`([a-z_]+)\s*=\s*"([^"]*)"`)
	mediaRE     = regexp.MustCompile(`!\[[^\]\n]{0,500}\]\(daedalus-media:[0-9a-f-]{36}\)`)
	cardOpenRE  = regexp.MustCompile(`(?i)^\s*:::\s*(card|entity)\b\s*(.*)$`)
	cardCloseRE = regexp.MustCompile(`^\s*:::\s*$`)
	cardFieldRE = regexp.MustCompile(`(?i)^\s*(title|url|image|badge|meta|links|note)\s*:\s?(.*)$`)
	flowOpenRE  = regexp.MustCompile(`(?i)^\s*:::\s*flow\b\s*(.*)$`)
	flowKeyRE   = regexp.MustCompile(`(?i)^\s*(title|note)\s*:\s?(.*)$`)
	flowStepRE  = regexp.MustCompile(`^\s*(↑↓|↓↑|⇅|⇵|↕|↓|↑|⬇\x{FE0F}?|⬆\x{FE0F}?|↧|↥)(?:\s+(.*?))?\s*$`)
	flowLinkRE  = regexp.MustCompile(`\s+(<->|↔|<-->|-->|->|→|<--|<-|←|\|)\s+`)
	flowItemRE  = regexp.MustCompile(`^\s*(?:[-*+]|\d+[.)])\s+`)
	flowMarkRE  = regexp.MustCompile(`==(\S(?:.*?\S)?)==`)
	siteRE      = regexp.MustCompile(`(?i)^https?://(?:www\d?\.|m\.)?([^/?#:]+)`)
	citeOneRE   = regexp.MustCompile(citeOne)
	citeRunRE   = regexp.MustCompile(citeOne + `(?:[ \t]*` + citeOne + `)*`)
	codeRE      = regexp.MustCompile("(?s)```.*?```|`[^`\n]*`")
	blankRunRE  = regexp.MustCompile(`\n{3,}`)
)

const citeOne = `\[\^([^\]\n]{0,40})\]\((https?://[^)\s]+)\)`

var flowStepASCII = map[string]string{"v": "↓", "^": "↑", "^v": "↕", "v^": "↕", "<->": "↕"}

var flowLinks = map[string]string{"<->": "↔", "<-->": "↔", "↔": "↔", "-->": "→", "->": "→", "→": "→", "<--": "←", "<-": "←", "←": "←", "|": "·"}

func site(url string) string {
	if m := siteRE.FindStringSubmatch(url); m != nil {
		return strings.ToLower(m[1])
	}
	return url
}

// Flatten turns an answer's rich syntax into plain Markdown: a card becomes its title as a link with
// its lines under it, a flow its rows one per line with arrows between them, citations the sites they
// name in brackets, an evidence tag its path or label, and an embedded media reference nothing (a
// terminal has no place for the picture).
func Flatten(text string) string {
	out := mediaRE.ReplaceAllString(evidenceRE.ReplaceAllStringFunc(flattenRich(text), evidence), "")
	return strings.TrimSpace(blankRunRE.ReplaceAllString(out, "\n\n"))
}

func evidence(tag string) string {
	m := evidenceRE.FindStringSubmatch(tag)
	attrs := map[string]string{}
	for _, pair := range attrRE.FindAllStringSubmatch(m[2], -1) {
		attrs[pair[1]] = pair[2]
	}
	if m[1] == "file" {
		path := strings.TrimSpace(attrs["path"])
		lines := strings.TrimSpace(attrs["lines"])
		if path != "" && lines != "" {
			return path + ":" + lines
		}
		return path
	}
	id := strings.TrimSpace(attrs["id"])
	label := strings.TrimSpace(attrs["label"])
	switch {
	case id == "":
		return label
	case label != "":
		return label + " (run " + id + ")"
	default:
		return "run " + id
	}
}

func flattenRich(text string) string {
	if !strings.Contains(text, ":::") && !strings.Contains(text, "[^") {
		return text
	}
	var out []string
	lines := strings.Split(text, "\n")
	fenced := false
	for index := 0; index < len(lines); {
		line := lines[index]
		if strings.HasPrefix(strings.TrimLeft(line, " \t"), "```") {
			fenced = !fenced
		}
		var opened, flow []string
		if !fenced {
			opened = cardOpenRE.FindStringSubmatch(line)
			if opened == nil {
				flow = flowOpenRE.FindStringSubmatch(line)
			}
		}
		if opened == nil && flow == nil {
			out = append(out, line)
			index++
			continue
		}
		var block []string
		index++
		for index < len(lines) && !cardCloseRE.MatchString(lines[index]) {
			block = append(block, lines[index])
			index++
		}
		index++
		if flow != nil {
			out = append(out, flowText(flow[1], block)...)
		} else {
			out = append(out, cardText(strings.ToLower(opened[1]), opened[2], block)...)
		}
		out = append(out, "")
	}
	// Citations are rewritten outside code only: a fence quoting the syntax keeps it as written.
	joined := strings.Join(out, "\n")
	var b strings.Builder
	last := 0
	for _, span := range codeRE.FindAllStringIndex(joined, -1) {
		b.WriteString(citeRunRE.ReplaceAllStringFunc(joined[last:span[0]], cites))
		b.WriteString(joined[span[0]:span[1]])
		last = span[1]
	}
	b.WriteString(citeRunRE.ReplaceAllStringFunc(joined[last:], cites))
	return b.String()
}

func cites(run string) string {
	var links []string
	for _, m := range citeOneRE.FindAllStringSubmatch(run, -1) {
		label := strings.TrimSpace(m[1])
		if label == "" {
			label = site(m[2])
		}
		links = append(links, "["+label+"]("+m[2]+")")
	}
	return " (" + strings.Join(links, ", ") + ")"
}

// cardText is one card as the lines a terminal reads: the title linked and the badge beside it, then
// the meta, the body as written, the links and the note. The picture is left out.
func cardText(kind, rest string, lines []string) []string {
	fields := map[string]string{"title": strings.TrimSpace(rest)}
	var body []string
	fenced := false
	for _, line := range lines {
		if strings.HasPrefix(strings.TrimLeft(line, " \t"), "```") {
			fenced = !fenced
		}
		var field []string
		if !fenced {
			field = cardFieldRE.FindStringSubmatch(line)
		}
		if field != nil {
			fields[strings.ToLower(field[1])] = strings.TrimSpace(field[2])
		} else {
			body = append(body, line)
		}
	}
	title, url := fields["title"], fields["url"]
	if !strings.HasPrefix(url, "https://") && !strings.HasPrefix(url, "http://") {
		url = ""
	}
	head := ""
	switch {
	case title != "" && url != "":
		head = "[" + title + "](" + url + ")"
	case title != "":
		head = title
	case url != "":
		head = "[" + site(url) + "](" + url + ")"
	}
	if head != "" {
		if kind == "card" {
			head = "**" + head + "**"
		} else {
			head = "### " + head
		}
	}
	if badge := fields["badge"]; badge != "" {
		if head != "" {
			head = head + " · _" + badge + "_"
		} else {
			head = "_" + badge + "_"
		}
	}
	var out []string
	for _, part := range []string{head, fields["meta"]} {
		if part != "" {
			out = append(out, part)
		}
	}
	if text := strings.TrimSpace(strings.Join(body, "\n")); text != "" {
		out = append(out, text)
	}
	if links := fields["links"]; links != "" {
		out = append(out, links)
	}
	if note := fields["note"]; note != "" {
		out = append(out, "_"+note+"_")
	}
	return out
}

// flowText is a flow as rows and arrows: the header bold, a row nobody joined to the one above gets
// the "↓" the app would draw, ASCII arrows become glyphs, the highlighted node's "==" marks become bold
// and the caption is muted.
func flowText(rest string, lines []string) []string {
	title, note := strings.TrimSpace(rest), ""
	var out []string
	pending := ""
	for _, line := range lines {
		if strings.TrimSpace(line) == "" || strings.HasPrefix(strings.TrimLeft(line, " \t"), "```") {
			continue
		}
		if key := flowKeyRE.FindStringSubmatch(line); key != nil {
			if strings.ToLower(key[1]) == "title" {
				title = strings.TrimSpace(key[2])
			} else {
				note = strings.TrimSpace(key[2])
			}
			continue
		}
		step := flowStepRE.MatchString(line)
		asciiStep, isASCII := flowStepASCII[strings.ToLower(strings.TrimSpace(line))]
		if step || isASCII {
			if step {
				pending = strings.TrimSpace(line)
			} else {
				pending = asciiStep
			}
			continue
		}
		row := strings.TrimSpace(flowItemRE.ReplaceAllString(line, ""))
		row = strings.TrimSpace(flowLinkRE.ReplaceAllStringFunc(" "+row+" ", func(link string) string {
			return " " + flowLinks[strings.TrimSpace(link)] + " "
		}))
		row = flowMarkRE.ReplaceAllString(row, "**$1**")
		if len(out) > 0 {
			if pending == "" {
				pending = "↓"
			}
			out = append(out, pending)
		}
		out = append(out, row)
		pending = ""
	}
	var head []string
	if title != "" {
		head = []string{"**" + title + "**"}
	}
	out = append(head, out...)
	if note != "" {
		out = append(out, "_"+note+"_")
	}
	return out
}
