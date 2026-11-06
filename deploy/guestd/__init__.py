"""The guest door: a process of its own, between a stranger's agent and everything this installation holds.

A link hands a conversation to somebody the operator does not control, and that somebody sits on the
far end of every answer: whatever a reader of their text can reach, they can simply ask for. So the
second party this installation has ever had lives here, in a process that was never given anything
worth asking for. It has its own port and its own database, no state volume, no secrets, no tools, and
no way to call the host: the host dials this process over a unix socket, never the other way round.

It lives beside the key proxy rather than in the ``daedalus`` package because it is another program, not
a part of the host: everything here imports only itself, the standard library and the web pieces
(starlette, uvicorn, httpx), and the host never imports it. In a container it runs with the rest of
Daedalus not even on the disk, which turns that rule from a convention into a fact.
"""
