"""Run the agent headless on recorded tasks and keep the evidence.

``daedalus-server bench`` drives sessions the way the chat does, without Telegram: each task gets a fresh
session and workspace, a prompt, an optional setup command and a check command whose exit code
decides pass or fail. Every task leaves one JSON record (status, pass, turns, tokens, cost, wall
time) and its trajectory, so a change to the agent can be priced instead of argued about. The
Harbor adapter in :mod:`daedalus.bench.harbor` plugs the same loop into public benchmarks.
"""
