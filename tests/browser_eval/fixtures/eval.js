// What a fixture page reports to the evaluation's server when the agent does something that is not a
// form's POST: a click on a div, a toggle, a choice in a widget. The server keeps it for the checker.
window.record = function (task, name, data) {
  return fetch("/event", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task: task, name: name, data: data === undefined ? null : data }),
  });
};
