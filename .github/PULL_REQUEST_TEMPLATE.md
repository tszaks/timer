## What and why

<!-- What does this change, and why? Link the issue if there is one. -->

## Checklist

- [ ] `python3 -m unittest discover -s tests -v` passes
- [ ] New behavior has a test
- [ ] If this touches claim, ack, nack, leases, cursors or compaction: the new test **fails without the change**
- [ ] The code matches the surrounding code and adds no dependency
- [ ] User-visible change: added a line under `[Unreleased]` in CHANGELOG.md
- [ ] README updated if a command, flag, environment variable or the JSON schema changed
