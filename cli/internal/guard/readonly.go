package guard

import (
	"path"
	"strings"
)

// ReadOnlyCommand reports whether a shell command only reads: every program in it is one that cannot
// change a file, run with no option that would make it, and no output is redirected anywhere but
// /dev/null. It answers false whenever it is unsure (a backtick, a here-document, a program it does
// not know), because plan mode runs what this calls read-only without asking and a wrong yes writes
// to the operator's disk.
//
// The server reads files through the shell too (tail, head, cut, wc, grep inside `if` and `for`), so
// the shell's own grammar, command substitution included, is followed far enough to let those
// through.
func ReadOnlyCommand(command string) bool { return readOnly(command, 0) }

const maxNesting = 4

func readOnly(command string, depth int) bool {
	if depth > maxNesting {
		return false
	}
	toks, subs, ok := tokenize(command)
	if !ok {
		return false
	}
	for _, sub := range subs {
		if !readOnly(sub, depth+1) {
			return false
		}
	}
	var simple []token
	flush := func() bool {
		ok := simpleReadOnly(simple, depth)
		simple = simple[:0]
		return ok
	}
	for _, t := range toks {
		if t.op {
			switch t.text {
			case "|", "||", "&&", ";", "(", ")":
				if !flush() {
					return false
				}
				continue
			case "&":
				// Something left running after the call returns is not a read the call can vouch for.
				return false
			}
		}
		simple = append(simple, t)
	}
	return flush()
}

// shellWords are the control words that carry no program of their own.
var shellWords = map[string]bool{
	"if": true, "then": true, "else": true, "elif": true, "fi": true, "do": true, "done": true,
	"while": true, "until": true, "!": true, "{": true, "}": true,
}

// harmless are builtins and programs that change nothing whatever their arguments.
var harmless = map[string]bool{
	":": true, "true": true, "false": true, "exit": true, "return": true, "break": true, "continue": true,
	"shift": true, "cd": true, "pwd": true, "echo": true, "printf": true, "test": true, "[": true, "[[": true, "]]": true,
	"set": true, "export": true, "local": true, "read": true, "wait": true, "unset": true,
	"ls": true, "cat": true, "head": true, "tail": true, "wc": true, "grep": true, "egrep": true, "fgrep": true,
	"rg": true, "ag": true, "which": true, "type": true, "command": true, "whoami": true, "id": true, "date": true,
	"uname": true, "hostname": true, "printenv": true, "file": true, "stat": true, "du": true, "df": true,
	"tree": true, "realpath": true, "readlink": true, "dirname": true, "basename": true, "diff": true, "cmp": true,
	"comm": true, "cut": true, "tr": true, "nl": true, "column": true, "fold": true, "rev": true, "tac": true,
	"od": true, "xxd": true, "hexdump": true, "strings": true, "sha1sum": true, "sha256sum": true,
	"sha512sum": true, "md5sum": true, "cksum": true, "base64": true, "jq": true, "seq": true, "expr": true,
	"paste": true, "join": true, "look": true, "locale": true, "nproc": true, "free": true, "uptime": true,
	"ps": true, "lsof": true, "sleep": true, "shasum": true,
}

// gitReads are the git subcommands that only read the repository.
var gitReads = map[string]bool{
	"status": true, "log": true, "diff": true, "show": true, "blame": true, "rev-parse": true, "ls-files": true,
	"ls-tree": true, "cat-file": true, "grep": true, "describe": true, "shortlog": true, "merge-base": true,
	"name-rev": true, "for-each-ref": true, "count-objects": true, "whatchanged": true, "version": true,
	"help": true, "rev-list": true, "show-ref": true, "check-ignore": true,
}

func simpleReadOnly(toks []token, depth int) bool {
	words := make([]string, 0, len(toks))
	for i := 0; i < len(toks); i++ {
		t := toks[i]
		if !t.op {
			words = append(words, t.text)
			continue
		}
		// A redirection and its target.
		if i+1 >= len(toks) || toks[i+1].op {
			return false
		}
		target := toks[i+1].text
		i++
		switch t.text {
		case "<", "<&":
		case ">&":
			if target != "-" && strings.Trim(target, "0123456789") != "" {
				return false
			}
		case ">", ">>", ">|", "&>", "&>>":
			if target != "/dev/null" {
				return false
			}
		default:
			return false
		}
	}
	// Control words and assignments come first and carry no program.
	for len(words) > 0 && (shellWords[words[0]] || isAssignment(words[0])) {
		words = words[1:]
	}
	if len(words) == 0 {
		return true
	}
	if words[0] == "for" {
		// `for name in words`: the words are data, and the body follows the next separator.
		return true
	}
	return programReadOnly(words, depth)
}

func programReadOnly(words []string, depth int) bool {
	prog := path.Base(words[0])
	args := words[1:]
	switch prog {
	case "env", "timeout", "nice", "nohup", "time":
		// They run the rest as a command of its own.
		rest := args
		for len(rest) > 0 && (strings.HasPrefix(rest[0], "-") || isAssignment(rest[0]) || (prog == "timeout" && isDuration(rest[0]))) {
			rest = rest[1:]
		}
		if len(rest) == 0 {
			return prog == "env"
		}
		return programReadOnly(rest, depth)
	case "bash", "sh", "zsh", "dash":
		for i, a := range args {
			if a == "-c" && i+1 < len(args) {
				return readOnly(args[i+1], depth+1)
			}
		}
		return false
	case "exec":
		// `exec 2>&1` only moves descriptors; with a program it would become that program.
		return len(args) == 0
	case "git":
		return gitReadOnly(args)
	case "sed":
		for _, a := range args {
			if a == "-i" || strings.HasPrefix(a, "-i") || strings.HasPrefix(a, "--in-place") {
				return false
			}
		}
		return true
	case "sort":
		for _, a := range args {
			if a == "-o" || strings.HasPrefix(a, "-o") || strings.HasPrefix(a, "--output") {
				return false
			}
		}
		return true
	case "uniq":
		// A second operand is an output file.
		operands := 0
		for _, a := range args {
			if !strings.HasPrefix(a, "-") {
				operands++
			}
		}
		return operands <= 1
	case "find":
		for _, a := range args {
			switch a {
			case "-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls":
				return false
			}
		}
		return true
	case "go":
		if len(args) == 0 {
			return false
		}
		switch args[0] {
		case "version", "list", "doc":
			return true
		case "env":
			for _, a := range args[1:] {
				if a == "-w" || a == "-u" {
					return false
				}
			}
			return true
		}
		return false
	}
	return harmless[prog]
}

func gitReadOnly(args []string) bool {
	// Global options before the subcommand: -C <dir>, -c <name=value> (a read-only config for the
	// one command), --no-pager and the like.
	for len(args) > 0 && strings.HasPrefix(args[0], "-") {
		if (args[0] == "-C" || args[0] == "-c") && len(args) > 1 {
			args = args[2:]
			continue
		}
		args = args[1:]
	}
	if len(args) == 0 {
		return false
	}
	sub, rest := args[0], args[1:]
	if gitReads[sub] {
		return true
	}
	onlyFlags := func(allowed ...string) bool {
		for _, a := range rest {
			ok := false
			for _, f := range allowed {
				if a == f {
					ok = true
				}
			}
			if !ok {
				return false
			}
		}
		return true
	}
	switch sub {
	case "branch":
		return onlyFlags("-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes", "--no-color")
	case "remote":
		return onlyFlags("-v", "--verbose")
	case "tag":
		return onlyFlags("-l", "--list")
	case "stash":
		return len(rest) > 0 && (rest[0] == "list" || rest[0] == "show")
	case "worktree":
		return len(rest) > 0 && rest[0] == "list"
	case "config":
		return len(rest) > 0 && (rest[0] == "--get" || rest[0] == "--list" || rest[0] == "-l" || rest[0] == "--get-all")
	case "reflog":
		return len(rest) == 0 || rest[0] == "show"
	}
	return false
}

func isAssignment(w string) bool {
	eq := strings.IndexByte(w, '=')
	if eq <= 0 {
		return false
	}
	for i, r := range w[:eq] {
		if !(r == '_' || r >= 'A' && r <= 'Z' || r >= 'a' && r <= 'z' || i > 0 && r >= '0' && r <= '9') {
			return false
		}
	}
	return true
}

func isDuration(w string) bool {
	return strings.TrimRight(strings.TrimLeft(w, "0123456789."), "smhd") == "" && w != ""
}

func isOperator(w string) bool {
	switch w {
	case "|", "||", "&&", ";", "&", ">", ">>", "<", "(", ")":
		return true
	}
	return false
}

// splitWords splits a command into words with operators as words of their own; false when the
// command uses a construct the tokenizer does not follow.
func splitWords(command string) ([]string, bool) {
	toks, _, ok := tokenize(command)
	if !ok {
		return nil, false
	}
	out := make([]string, len(toks))
	for i, t := range toks {
		out[i] = t.text
	}
	return out, true
}

type token struct {
	text string
	op   bool
}

// tokenize splits a POSIX shell command into words and operators, and returns the bodies of its
// command substitutions for the caller to check on their own. It refuses (ok false) backticks,
// process substitution and here-documents: they are rare in what an agent runs, and following them
// properly is more grammar than a guard should trust itself with.
func tokenize(s string) (toks []token, subs []string, ok bool) {
	var word strings.Builder
	inWord := false
	flushWord := func() {
		if inWord {
			toks = append(toks, token{text: word.String()})
			word.Reset()
			inWord = false
		}
	}
	emitOp := func(op string) {
		flushWord()
		toks = append(toks, token{text: op, op: true})
	}
	rs := []rune(s)
	n := len(rs)
	// substitution reads a balanced $( ... ) starting at the '(' at i and returns the index of its
	// closing parenthesis, or -1.
	substitution := func(i int) int {
		depth := 0
		quote := rune(0)
		for j := i; j < n; j++ {
			c := rs[j]
			switch {
			case quote == '\'':
				if c == '\'' {
					quote = 0
				}
			case quote == '"':
				if c == '\\' {
					j++
				} else if c == '"' {
					quote = 0
				}
			case c == '\\':
				j++
			case c == '\'' || c == '"':
				quote = c
			case c == '(':
				depth++
			case c == ')':
				depth--
				if depth == 0 {
					return j
				}
			}
		}
		return -1
	}
	for i := 0; i < n; i++ {
		c := rs[i]
		switch {
		case c == '\\':
			if i+1 < n {
				if rs[i+1] != '\n' {
					word.WriteRune(rs[i+1])
					inWord = true
				}
				i++
			}
		case c == '\'':
			inWord = true
			j := i + 1
			for j < n && rs[j] != '\'' {
				j++
			}
			if j >= n {
				return nil, nil, false
			}
			word.WriteString(string(rs[i+1 : j]))
			i = j
		case c == '"':
			inWord = true
			j := i + 1
			for ; j < n && rs[j] != '"'; j++ {
				switch {
				case rs[j] == '\\' && j+1 < n:
					j++
					word.WriteRune(rs[j])
				case rs[j] == '`':
					return nil, nil, false
				case rs[j] == '$' && j+1 < n && rs[j+1] == '(':
					end := substitution(j + 1)
					if end < 0 {
						return nil, nil, false
					}
					if !(j+2 < n && rs[j+2] == '(') {
						subs = append(subs, string(rs[j+2:end]))
					}
					word.WriteString(string(rs[j : end+1]))
					j = end
				default:
					word.WriteRune(rs[j])
				}
			}
			if j >= n {
				return nil, nil, false
			}
			i = j
		case c == '`':
			return nil, nil, false
		case c == '$' && i+1 < n && rs[i+1] == '(':
			end := substitution(i + 1)
			if end < 0 {
				return nil, nil, false
			}
			// $(( )) is arithmetic, data and not a command.
			if !(i+2 < n && rs[i+2] == '(') {
				subs = append(subs, string(rs[i+2:end]))
			}
			word.WriteString(string(rs[i : end+1]))
			inWord = true
			i = end
		case c == '#' && !inWord:
			for i < n && rs[i] != '\n' {
				i++
			}
			i--
		case c == ' ' || c == '\t':
			flushWord()
		case c == '\n' || c == ';':
			if c == ';' && i+1 < n && rs[i+1] == ';' {
				return nil, nil, false // case arms
			}
			emitOp(";")
		case c == '|':
			if i+1 < n && rs[i+1] == '|' {
				emitOp("||")
				i++
			} else if i+1 < n && rs[i+1] == '&' {
				return nil, nil, false
			} else {
				emitOp("|")
			}
		case c == '&':
			switch {
			case i+1 < n && rs[i+1] == '&':
				emitOp("&&")
				i++
			case i+2 < n && rs[i+1] == '>' && rs[i+2] == '>':
				emitOp("&>>")
				i += 2
			case i+1 < n && rs[i+1] == '>':
				emitOp("&>")
				i++
			default:
				emitOp("&")
			}
		case c == '(' || c == ')':
			if c == '(' && inWord {
				return nil, nil, false // a function definition or an array
			}
			emitOp(string(c))
		case c == '>' || c == '<':
			// A word of digits right before is the descriptor the redirection applies to.
			if inWord && strings.Trim(word.String(), "0123456789") == "" {
				word.Reset()
				inWord = false
			} else {
				flushWord()
			}
			if i+1 < n && rs[i+1] == '(' {
				return nil, nil, false // process substitution
			}
			op := string(c)
			if i+1 < n {
				next := rs[i+1]
				switch {
				case c == '>' && (next == '>' || next == '|' || next == '&'):
					op += string(next)
					i++
				case c == '<' && next == '<':
					return nil, nil, false // here-document or here-string
				case c == '<' && next == '&':
					op += "&"
					i++
				}
			}
			toks = append(toks, token{text: op, op: true})
		default:
			word.WriteRune(c)
			inWord = true
		}
	}
	flushWord()
	return toks, subs, true
}
