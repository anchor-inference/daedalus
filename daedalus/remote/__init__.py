"""Remote conversations: the terminal client's devices, what their tokens reach, and the live mark.

The contract with the client is ``docs/architecture/cli.md``. A conversation started from the terminal
client is of kind ``remote``; its tools run on the device that started it, in the folder it was started
in. This package keeps the devices and their pairing (:mod:`.devices`), the door that checks a device's
credential before any route runs (:mod:`.door`), and which sessions a connected terminal shows now
(:mod:`.control`).
"""
