package rpc_test

import (
	"fmt"
	"net/http"
)

// devtoolsPages are the fixture's pages for what an agent building a site reads: the console, the
// requests, the dialogs answered for it, and why an element does not show.
func devtoolsPages(mux *http.ServeMux) {
	doc := func(path, body string) {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			fmt.Fprint(w, "<!doctype html><html><head><meta charset=\"utf-8\"></head><body style=\"margin:0;font:16px sans-serif\">"+body+"</body></html>")
		})
	}
	// The console: a formatted line, an object one level deep, a warning, a caught error logged, a
	// line three times, an uncaught error, a rejected promise, a picture that is not there — and an
	// object whose getter would say so in the title if logging it ran the page's code.
	doc("/devtools/console", `<title>Console</title><img src="/devtools/missing.png" alt="gone">`+
		`<button id="b" style="width:200px;height:40px" onclick="console.error('clicked and failed:', 42); document.getElementById('m').textContent='pressed'">Break</button><p id="m">idle</p>`+
		`<script>
		console.log("hello %s", "world", {a: 1, b: "x"});
		console.log("%cstyled", "color: red", "after");
		console.warn("careful");
		console.debug("chatter");
		for (let i = 0; i < 3; i++) console.log("again");
		const trap = {};
		Object.defineProperty(trap, "k", {enumerable: true, get() { document.title = "getter ran"; return 1; }});
		const err = new Error("trap");
		Object.defineProperty(err, "stack", {get() { document.title = "getter ran"; return "S"; }});
		console.log(trap);
		console.log(err);
		setTimeout(() => { null.boom(); }, 0);
		Promise.reject(new Error("nobody caught this"));
		</script>`)
	// Dialogs: an alert after a click, a confirm that stays the agent's, a question before leaving.
	doc("/devtools/dialogs", `<title>Dialogs</title>`+
		`<button id="save" style="width:200px;height:40px" onclick="alert('Saved!'); document.getElementById('m').textContent='saved'">Save</button>`+
		`<button id="del" style="width:200px;height:40px" onclick="document.getElementById('m').textContent = confirm('Delete it?') ? 'deleted' : 'kept'">Delete</button>`+
		`<p id="m">unsaved</p>`+
		`<script>addEventListener("beforeunload", (e) => { e.preventDefault(); e.returnValue = ""; });</script>`)
	// Requests: an API behind the page, answering with credentials in every place a site puts them.
	doc("/devtools/app", `<title>App</title>`+
		`<button id="load" style="width:200px;height:40px" onclick="load()">Load</button>`+
		`<button id="login" style="width:200px;height:40px" onclick="login()">Sign in</button><ul id="list"></ul>`+
		`<script>
		async function load() {
			const r = await fetch("/devtools/items?page=2&access_token=SECRET-query", {headers: {"Authorization": "Bearer SECRET-auth", "X-Api-Key": "SECRET-apikey", "X-Trace": "t-1"}});
			const data = await r.json();
			for (const it of data.items) { const li = document.createElement("li"); li.textContent = it.name; document.getElementById("list").append(li); }
		}
		async function login() {
			await fetch("/devtools/login", {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: "user=someone&password=SECRET-typed"});
		}
		</script>`)
	mux.HandleFunc("/devtools/items", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		http.SetCookie(w, &http.Cookie{Name: "sid", Value: "SECRET-cookie"})
		w.Header().Set("X-Request-Id", "req-7")
		fmt.Fprint(w, `{"items":[{"name":"Blue mug","sku":"BM-1","code":"MUG"}],"session_token":"SECRET-json","auth":{"refresh_token":"SECRET-nested"},"next":"/devtools/items?page=3"}`)
	})
	mux.HandleFunc("/devtools/login", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"ok":true,"token":"SECRET-login"}`)
	})
	mux.HandleFunc("/devtools/gone", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, "<title>Not here</title>not here")
	})
	// Why an element does not show, in each of the ways a page hides one.
	doc("/devtools/inspect", `<title>Inspect</title>
		<div id="panel" style="display:none"><button id="inpanel">Hidden in a panel</button></div>
		<button id="ghost" style="opacity:0;width:120px;height:30px">Ghost</button>
		<button id="under" style="position:absolute;left:10px;top:60px;width:160px;height:40px">Under a veil</button>
		<div id="veil" style="position:absolute;left:0;top:50px;width:400px;height:80px;background:rgba(0,0,0,.4)">please wait</div>
		<button id="away" style="position:absolute;left:-9999px;top:0">Placed away</button>
		<div id="box" style="position:absolute;left:10px;top:150px;width:100px;height:30px;overflow:hidden"><button style="margin-left:300px">Cut off</button></div>
		<div id="pane" style="position:absolute;left:10px;top:200px;width:200px;height:60px;overflow:auto"><div style="height:400px"></div><button id="deep">Deep in a pane</button></div>
		<button id="off" disabled style="position:absolute;left:10px;top:280px">Disabled</button>
		<button id="nothru" style="position:absolute;left:200px;top:280px;pointer-events:none">Clicks pass</button>
		<form style="position:absolute;left:10px;top:320px"><input type="hidden" name="csrf_token" value="SECRET-csrf"><input type="password" value="SECRET-pw"><input name="q" value="shoes"><button type="button" onclick="steal()" style="color:rgb(1, 2, 3)">Fine</button><script>function steal(){}</script></form>`)
}
