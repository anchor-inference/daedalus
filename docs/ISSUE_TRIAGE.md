# Issue triage

An issue needs a reproducible trigger, the affected version and platform, requested versus observed settings, and the outcome. Ask for a smaller reproduction when logs contain private data; use the private path in `SECURITY.md` for vulnerabilities.

Use these states when tracking a bug:

- **Needs reproduction:** the failure is reported but the trigger or evidence is incomplete.
- **Confirmed:** a maintainer reproduced it on a named version and platform.
- **Partial:** a change addresses some cases, while a stated reproducer still fails.
- **Fixed on main:** the reproducer passes at a specific commit. This says nothing about an installed release.
- **Released:** the exact package version containing the fix was built and checked on the affected platform.

Link duplicates to the earliest issue with the useful reproducer and preserve any distinct version or platform evidence. Close an issue as released only after its package and platform have been checked; a passing source test alone supports **Fixed on main**. For adapter problems, include both the requested and the observed model, tools, permission, and resume behavior. Avoid promising support for a platform or adapter version that has only been built, not run.
