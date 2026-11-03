# Security

MetaSoundTools adds tools to the MCP server built into Unreal Editor 5.8. This
page says what those tools can reach, what they can't, and what is left to you.

Supported: UE 5.8. This code is tested on 5.8.4. Earlier revisions also ran on
5.8.3. Epic labels the MCP server Experimental, so expect these details to
change in later engine versions.

## Save your work first

An agent driving these tools can still hang or crash the editor, and a crash
loses unsaved work. Tool calls run on the editor's game thread, so one engine
dialog blocks every later call until someone clicks it. The toolset checks what
it can before it touches the engine, but these are the main open cases, and the
list isn't exhaustive:

- Extreme values on engine nodes. Setting Grain Delay's `Max Delay Time` to
  100000 crashed the editor on 5.8.4 when the graph played. The toolset can't
  know every node's safe range.
- A recording nobody finishes grows in memory, about 1.4 GB an hour at 48 kHz
  stereo and faster with more output channels, until the toolset stops and
  exports it at the 20-minute cap. Up to that point it can take several
  hundred megabytes. The cap runs on editor ticks and survives a toolset
  reload. It can't stop a recording while Play In Editor runs, because the
  engine mutes the editor world and nothing the toolset can reach gets to the
  recorder then; it stops the recording on the first tick after play ends.
  On 5.8.4 with default play settings the capture didn't grow during play,
  since the engine flushes the editor's sounds when play starts. That was
  measured once, headless, and other play settings weren't tested.
- The engine writes every WAV through one shared writer, and a stop while it
  is busy can cancel the file it is writing. So while an export holds it, a
  finish is refused and its recording keeps running, listed and under the
  cap, until a retry goes through. The cap waits the same way. The writer is
  released after the engine reports that export done. The release comes two
  editor ticks after the report: the engine frees the writer only after
  every listener to that report has run, so a script that finishes a
  recording from inside the report is refused too. The exception is a
  script that opens a dialog or otherwise runs the editor's loop from inside
  the report: editor ticks can then pass before the engine frees the
  writer, so that finish can go through and its file can be lost. The
  second tick is for the gap below. The report names no export, and the
  engine's own Start/Finish Recording Submix Output, which any Blueprint
  can call, sends the same report on the same submix from a writer of its
  own. So a report only counts when that export's own WAV is already
  complete, and only the first one that counts sets the release; later ones
  don't move it. The engine also reports a write that failed, and that
  report arrives without counting. If the failed write left no file, the
  first guess below releases the writer. If it left a partial file, the
  writer stays held until that file is deleted or moved, which hands it to
  the same guess. An export whose output folder couldn't be prepared sends
  no report at all and falls to that guess too. One gap is left: if that
  other API finishes a WAV on the same submix in the moment after the
  toolset's file is complete but before the toolset's own report arrives,
  the writer can be released early, and the next export can be cancelled if
  the toolset's report comes more than two ticks after that other one. That
  API also stops the very recorder a toolset recording on that submix is
  using, so don't use both on one submix at once.
- Master-output recordings start through the master submix object that
  Project Settings > Audio > Master Submix names, the one the audio device
  loaded, and every recording stops through the very submix object it
  started with, held while it runs, never a path looked up again. So
  renaming a submix mid-recording, or moving another into its old path,
  doesn't redirect the stop. If that setting is changed while the editor
  runs (the engine asks for a restart), master recordings until the restart
  can address a submix the device doesn't treat as its master. The
  capture can come out empty, and in one sequence read from engine source
  but not fired, a recorder can be left running outside the cap. Restart
  the editor after changing it, as the engine asks.
- A recording or export still running when the toolset's Python is
  reloaded in place over an earlier build gets weaker handling. Since 0.1.0
  is the first release, the only earlier builds in existence are
  pre-release ones. The export
  is released by the second guess below, since the reloaded code can't
  tell which reports the old code counted. A master recording from such a
  build stops through the engine's own fallback to its master, which
  reaches the right recorder but sends no report, so it takes the same
  guess. One on a chosen submix finds that submix by its path again.
- Two releases of the writer are guesses. An export whose file hasn't
  appeared after 2 seconds, plus 0.02 seconds for every second the recording
  ran, and two editor ticks, is taken to be an empty capture, which the
  engine never writes; a slow export mistaken for one can still be cancelled
  by the next. And when the toolset can't subscribe to an export's report,
  or the export dates from before an in-place reload (the bullet above), it
  waits two ticks and a tenth of a second after the file is complete,
  which the engine's worker can still outlast. A writer held for 5 minutes
  is logged and stays held, whether or not its report has come. If that
  export failed, deleting or moving its file lets the first guess release
  it, but only once its full wait has passed: after a six-hour recording
  that is 434 seconds after the stop, so deleting the file at the 5-minute
  warning doesn't free the writer at once.
- On a source-controlled project, `build_to_asset` can open a check-out
  dialog, and `create_attenuation_asset` can open the engine's deleted-package
  dialog. Both are from reading engine source; neither has been tested.

## What you're installing

This is an editor plugin. Its Python runs at editor startup, inside the editor
process, with your user account's rights. Nothing sandboxes it. Read the code
before you install it, as you would with any plugin that runs at startup.

## The server is Epic's

This plugin opens no port and handles no network traffic. Epic's server does
that, and Epic's docs are plain about it: "There is no authentication layer; the
plugin is not safe to expose beyond the local machine."

Tested on 5.8.4 with the bind settings at their defaults:

- The server listened on `127.0.0.1` only, with no IPv6 listener.
- Requests with an `Origin` from another site were refused with 403, including
  lookalikes such as `http://localhost.evil.example` and `Origin: null`.

What that design leaves open:

- Any process running on your machine can connect and call every tool.
- The `Origin` check accepts `localhost`, `127.0.0.1` and `[::1]` on any port and
  any scheme. Browsers should still be stopped there, because the server answers
  no CORS preflight, so a web page shouldn't be able to list or call tools. That
  comes from reading the source; it hasn't been tested in a browser.

Don't change `DefaultBindAddress`, add a `ListenerOverrides` entry, port-forward
the server or tunnel it. The server also doesn't separate clients: every
connected client sees the same builders, auditions and recordings.

**The limits below bind this toolset, not the connection.** The same server
offers every other toolset you've enabled. If Epic's EditorToolset is on, a
connected agent can delete assets with `AssetTools.delete` whatever this page
says. It can also use `AssetTools.write_file` to change text files, `.py`
included, under project content, Saved and any enabled plugin's `Content` folder,
engine plugins among them. That reaches this plugin's Python, which runs at the
next editor start.

With Epic's tool search on, which is the default, your client sees every tool as
one `call_tool`, so approving that tool approves all of them. 5.8's tool schema
has no read-only or destructive marking either. The control that exists is on
the server: the `BlockedNames` and `AllowedNames` lists in the Toolset Registry
settings.

## What the tools can do

| Area | Reach |
|---|---|
| Read assets | Any asset under any mounted content root, engine content included. Reading can load the asset |
| Write assets | Anything mounted outside the engine install. Writes to engine content are refused |
| Change existing assets | `set_sound_routing` changes routing on a sound asset and saves it. `attach_builder_to_asset` edits a MetaSound. Both refuse engine content |
| Overwrite | MetaSounds only, only with `overwrite=true`, and refused while a builder holds a node of the target, an unsaved edit made through these tools this session holds one, or the asset registry shows a saved hard reference to it. Soft references don't count, and neither do edits made outside these tools, edits from before a toolset reload, or anything the registry hasn't scanned yet, so keep your own version control. The result is a new asset, and routing isn't carried over. The old asset is backed up during the swap and the backup is normally dropped on success |
| Open editors | Audio assets only |
| Write files | WAVs in `Saved/MetaSoundToolset/Renders`, never pruned. Starting a render deletes an older WAV of the same name there |
| Read files | `.wav` files under the project and its Saved folder, measured only. The path is checked on its spelling before anything is opened, so a network or device path that names somewhere outside the project is refused without a connection being made. A project that itself lives on a network share is read over that share, as the editor reads it. A link inside the project that leads out is refused after it is resolved, and resolving it can touch whatever it points at. Results are levels and duration, never file contents. Error messages show whether such a file exists |
| Sound | Plays through your speakers with no level limit. An audition keeps playing until it's stopped, even after a client disconnects. Recording captures the master output by default, which is everything the editor plays |
| Everything else | The plugin's code opens no sockets and makes no network connection of its own, and it runs no subprocesses, `eval` or `exec`. File and asset access goes through ordinary paths and engine mounts, so a project or mount on a network share is reached over the network like any other read of it. The WAV analysis refuses a network path outside the project before opening it |

Graphs run engine nodes, and a node can do more than this table says. Wave
Writer, for one, writes a WAV to a path built from its `Filename Prefix`
input, and that path isn't confined to the project: a prefix that spells a
network path is an outbound connection made by the graph when it plays. The
toolset doesn't filter that pin, because a graph input or a parameter can
feed it just as well, so a filter there would be a false promise.

The plugin's own tests create and delete files under `Saved/` and a temporary
content mount when someone runs them.

The toolset can't tell whether an asset is open in an editor, because the
subsystem it would ask has no Python accessor. Close an asset's editor before
changing it from an agent. Epic's `GetOpenAssets` tool lists them if you want to
check.

## What it doesn't protect you from

**Prompt injection through assets.** Asset names, graph input names, pin names
and String defaults come from whatever is in your project, including packs made
by other people. Graph reads pass that text to the model. Default values are cut
at 512 characters on those reads, though the typed getters return all of it, and
names aren't cut at all beyond the engine's own 1023-character limit. The
toolset description and the bundled skill tell the agent this text is data, when
they reach it: with tool search off the toolset description isn't sent, and the
skill only counts if the client loads it. That is a label, not a filter. A hostile
asset can still try to steer an agent, and a model can still be fooled.

**What goes to your LLM provider.** Tool results go to whichever model your
client uses. They include asset names and absolute file paths, which can contain
your username. Epic's server logs a notice that editor data sent this way is
Licensed Technology under section 6(e) of the UE EULA, and that keeping it out of
provider training is your responsibility.

**Telemetry.** Epic's server records session IDs, tool name hashes, durations and
success through the editor's analytics. The hashes are of public names and Epic
ships a tool to decode them, so read them as names. That is Epic's code, not this
plugin's. `ModelContextProtocol.EnableAnalytics` or the editor's Send Usage Data
setting turns it off.

**Audit logs.** This plugin keeps no log of tool calls. Epic's server logs each
tool name at `Log` level, with parameters and results at `VeryVerbose`, in the
editor log. A tool error that quotes an argument lands in the log at `Warning`.

## Checking the tools haven't changed

Tool descriptions are instructions to a model, so a changed description matters
as much as changed code. `Content/Python/metasound_toolset/data/tool_surface_pin.json`
holds a SHA-256 of the toolset schema, hashed from the same text the server
gives clients when no `BlockedNames` or `AllowedNames` filter applies to this
toolset. The test suite and the startup check hash the toolset unfiltered; the
server describes it with those filters applied, so with a filter on, what
clients see can differ from what was pinned. On the pinned engine version the
test suite fails if the code and the pin disagree; on any other version it
skips that comparison. The editor log prints the running hash at startup.

To check a running editor:

```bash
python tools/verify_tool_surface.py --port 8000
```

That compares against the pin in your copy, and a pin in your copy can't catch
much. A bad update ships a new pin too, and a connected agent that can write
this plugin's files can rewrite the pin along with the code. To catch those, save
the hash when you first review the tools, then compare against it later:

```bash
python tools/verify_tool_surface.py --port 8000 --expected <hash you saved>
```

Read a match narrowly. The hash covers this toolset's tool names, schemas and
descriptions on one engine version, because the engine generates part of the
schema text. It doesn't cover what the tools do, the bundled skill, catalog text
or anything else registered on the server. It's a tripwire for interface changes,
not proof the tools are safe. The script needs Epic's tool search on, and the
Toolset Registry's `BlockedNames` and `AllowedNames` lists can change the hash if
they filter this toolset.

## Dependencies and releases

There are no third-party Python packages. The code imports the Python standard
library, the engine's `unreal` module and Epic's `toolset_registry`. The plugin
declares three engine plugins: ToolsetRegistry, Metasound and PythonScriptPlugin.
There's nothing here for a dependency scanner to check beyond the engine itself.

A release is a git tag, and the tag's release notes carry two numbers: the
SHA-256 of the source archive, and the tool surface hash for the engine
version that release was tested on. For 0.1.0, tested on 5.8.4, the tool
surface hash is
`c8d4966a4b479ad0f5e91212b073233f0d239d3e23352fe3f4ceba5f07de895d`.

## Reporting a vulnerability

Report privately through the repository's **Security** tab ("Report a
vulnerability"); don't open a public issue.
Include the engine version and what an attacker would need to already
have. This is a free project kept by one person, so there's no response-time
promise.

Issues in Epic's MCP server belong with Epic.
