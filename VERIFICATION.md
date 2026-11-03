# How this release was verified

This page says what was actually checked before 0.1.0 shipped, on what, and
where the limits of that checking are. [SECURITY.md](SECURITY.md) is the
companion page: what the tools can reach and what stays your responsibility.

## The test suite

The release code passed its full suite, 246 tests, on **two different
UE 5.8.4 projects** with different plugin and content configurations: one
close to a plain project, one carrying Game Features and asset-reference
restrictions. Runs are headless (`-NullRHI`, `-Unattended`), each on a
fresh editor process.

Two details make those numbers worth more than a green summary line:

- **Per-test verdicts are counted, not assumed.** Epic's automation runner
  polls each Python test's result for five seconds by default, and a poll
  that times out is recorded as a pass while the tests queued behind it
  never run. The suite harness raises that to 120 seconds per test, and the
  run evidence counts the editor log's own per-test unittest verdicts
  against the number of tests. A green run here means all 246 verdicts were
  present and OK on each project, not that a controller summary said so.
- **Regression tests are shown failing first.** Tests written for a fix are
  run against the pre-fix code and must fail at their intended assertion
  before the fix counts as covered.

Running the suite yourself: the command is in the
[README](README.md#tests). On a project that restricts asset references
(Game Features, for example), four reference-save tests skip by design, and
an engine-side reference-domain ensure has been seen to fail one test
intermittently there; the same run repeated at the same code came back
clean. One further test needs to create a symbolic link, to prove that a
link leading out of the project is refused; on Windows without the symlink
privilege (or Developer Mode) it skips instead.

## Independent review

Before release, the source went through **ten rounds of independent cold
review**: a separate model, unaffiliated with the code's authors, reading
frozen snapshots of the full source and of each fix round's diffs, under a
written exit condition fixed in advance. Review alternated with bounded fix
rounds, every finding was verified against the code before being acted on,
and the final round returned zero findings at any severity. The arc's
security-relevant failure scenarios were reproduced live over MCP on fresh
editors when first found, and re-run as refusals after their fixes.

## Live verification

Not everything can be proven headless, so the release also had attended
time on a live 5.8.4 editor:

- Renders were listened to by a human, and an asset-attached render was
  compared bit-identical (SHA-256) against the audition path's render.
- The dialog-opening cases and the Play In Editor refusal were exercised
  live; the editor-focus mute's effect on renders was measured on two
  machine configurations.
- The node catalog's entries were resolved against a live editor's node
  registry: 48 of its 50 classes verified, and a graph using the
  hardest-to-name four rendered with real signal.

## The tool surface pin

The schema an agent reads is pinned by hash
(`Content/Python/metasound_toolset/data/tool_surface_pin.json`), checked by
the suite on the pinned engine version and printed by the editor log at
startup. `tools/verify_tool_surface.py` checks a running editor against it.
[SECURITY.md](SECURITY.md#checking-the-tools-havent-changed) says what that
hash does and does not cover.

## The limits

- Tested on 5.8.4; earlier revisions also ran on 5.8.3. Other engine
  versions are untested, and the suite deliberately skips its pin
  comparison off the pinned version.
- Two projects is two projects. Plugin combinations beyond those
  configurations, source-control dialogs, and non-default audio device
  setups are not covered; the known-untested cases are called out in
  SECURITY.md.
- Measurement is not listening. The suite proves levels, durations and
  silence, and a human spot-checked renders by ear; it cannot prove a sound
  is the sound you wanted.
- "Verified" in the node catalog means verified on 5.8.4 on the date the
  entry records, nothing more.
