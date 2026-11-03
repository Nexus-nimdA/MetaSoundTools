# MetaSoundTools

**The first dedicated, sound-designer-grade audio toolset for Epic's UE MCP.**

MetaSoundTools is a free Unreal Engine 5.8 editor plugin that gives an AI
agent working ears and hands for MetaSounds. Through the editor's built-in
MCP server, an agent can build a graph, play it in the editor, change it
while it plays, render it to a WAV and measure what actually came out, then
save it as an asset a human can open and read.

I'm Dean Cristofori, a sound designer with over a decade in AAA. Agents
can't hear, and audio shouldn't ship on faith. This gives your agent the
ability to check its audio work, and gives sound designers an agent that
actually earns its seat. Hand the wiring to it overnight and come back to
placeholders hooked up where you want them, so you can drop your designed
sounds right in and be off to the races.

MIT licensed, see [LICENSE](LICENSE). Not affiliated with or endorsed by
Epic Games.

> **Experimental, like the server it plugs into.** The MCP server ships in
> UE 5.8 as an Experimental feature and its APIs may change between engine
> versions. MetaSoundTools targets UE 5.8 and was tested on 5.8.4.

## The idea

An agent that writes MetaSounds blind will happily ship you silence. The
loop this toolset is built around closes that gap:

1. **Build** a source or patch through the MetaSound Builder API.
2. **Hear it.** `start_audition` plays the graph in the editor and returns
   immediately, so the agent keeps working while sound plays. Live updates
   stay on: `set_audition_param` and `pulse_audition_trigger` reshape a
   playing sound, `stop_audition` ends it. A sine can be created, heard,
   pushed up an octave and stopped without a single save.
3. **Measure it.** An agent cannot listen, so it measures instead. This is
   audio's version of taking a screenshot to check your work:
   `start_source_render` and `finish_source_render` capture a graph to a
   WAV, and `analyze_rendered_wav` reports duration, peak, RMS in dB, and
   whether the file is silent. A silent render gets caught before anyone
   ships it. `start_output_recording` captures everything the editor plays.
4. **Save it readable.** `build_to_asset` writes the asset with a
   left-to-right node layout, so the generated graph opens legible for the
   human who inherits it.

Around that loop sit the supporting pieces:

- **Full Builder authoring.** Sources (playable sounds) and patches
  (reusable sub-graphs); nodes added by class name or pulled from a saved
  MetaSound; wiring; typed graph inputs and outputs with defaults;
  interfaces added and removed. Every setter has a getter that reads the
  value back from the engine rather than echoing the request.
- **Real node names, not guessed ones.** Guessed class names are the most
  common way an agent's MetaSound fails. `search_node_classes` searches a
  curated catalog of 50 node classes, 48 of them verified against a live
  5.8.4 editor, and each result says whether it was. `resolve_node_class`
  checks any name against the engine's live registry and returns the
  node's real pins.
- **One call for the common jobs.** `create_random_oneshot` builds a
  pick-a-wave player with pitch and volume variation;
  `create_wave_player` builds a source with dB gain and pitch inputs. Both
  validate before touching the engine, so a bad call leaves nothing
  half-built.
- **Routing.** Read and set a sound's attenuation, sound class and submix;
  create spherical attenuation assets.
- **A bundled Agent Skill.** `MetaSoundGraphCraft` registers at editor
  startup and teaches an agent the conventions a tool schema cannot carry:
  there is no Gain node, so volume goes through a dB-to-linear multiply;
  one-shots must carry the OneShot interface and fire its finished trigger
  or the sound leaks; some node families name their classes in ways nobody
  would guess.

## The tools

54 tools in one toolset.

| Area | Tools |
|---|---|
| Discovery | `list_metasounds`, `describe_metasound`, `search_node_classes`, `resolve_node_class` |
| Builders | `create_metasound_source`, `create_metasound_patch`, `attach_builder_to_asset`, `release_builder`, `list_builders`, `get_graph_info` |
| Nodes and wiring | `add_node`, `add_node_from_asset`, `remove_node`, `describe_node`, `connect_nodes`, `disconnect_nodes`, `set_node_input_default`, `get_node_input_default` |
| Graph inputs and outputs | `add_graph_input`, `remove_graph_input`, `add_graph_output`, `remove_graph_output`, `set_graph_input_default`, `get_graph_input_default`, `connect_to_graph_output`, `connect_from_graph_input` |
| Interfaces | `add_interface`, `remove_interface`, `has_interface` |
| Audition | `start_audition`, `audition_asset`, `set_audition_param`, `pulse_audition_trigger`, `get_audition_status`, `list_auditions`, `stop_audition`, `stop_all_auditions` |
| Render and measure | `start_source_render`, `finish_source_render`, `start_output_recording`, `finish_output_recording`, `list_output_recordings`, `analyze_rendered_wav` |
| Save and view | `build_to_asset`, `save_builder_asset`, `auto_layout_nodes`, `open_in_editor` |
| Routing | `get_sound_routing`, `set_sound_routing`, `create_attenuation_asset`, `describe_attenuation`, `list_audio_routing_assets` |
| Templates | `create_random_oneshot`, `create_wave_player` |

Every tool's description is written for the agent that will call it: its
arguments, its return value, and every way it refuses.

## Install

You need Unreal Engine 5.8 with Epic's **Unreal MCP** plugin
(`ModelContextProtocol`) enabled in your project. MetaSoundTools declares
the other three plugins it needs (Toolset Registry, MetaSound, Python
Editor Script Plugin), and the engine enables them with it.

1. Put this repository in your project's `Plugins` folder as
   `Plugins/MetaSoundTools`:

   ```bash
   git clone https://github.com/Nexus-nimdA/MetaSoundTools.git Plugins/MetaSoundTools
   ```

2. Restart the editor. The plugin is enabled by default; nothing needs
   adding to your `.uproject`. The Output Log confirms with
   `MetaSoundToolset: registered MetaSoundsToolset.` and the tool surface
   hash.

3. Start the MCP server: run `ModelContextProtocol.StartServer` in the
   editor console, or turn on **Auto Start Server** under Editor
   Preferences > General > Model Context Protocol. The default endpoint is
   `http://127.0.0.1:8000/mcp`.

4. Point your MCP client at the server.
   `ModelContextProtocol.GenerateClientConfig` in the editor console
   writes a client config to the project root; Epic's Unreal MCP
   documentation lists supported clients.

With Epic's tool search on (the default), an agent finds the toolset
through `list_toolsets` as `metasound_toolset.toolset.MetaSoundsToolset`,
reads it with `describe_toolset`, and calls tools through `call_tool`.

A naming note: the public name is MetaSoundTools. Inside the engine the
toolset registers as `MetaSoundsToolset`, from the Python package
`metasound_toolset`, and log lines, the test path and the render folder
use `MetaSoundToolset`.

## Try it

Ask your agent something like:

> Build a MetaSound source that plays a 440 Hz sine, audition it, then
> raise it an octave while it plays. Render two seconds of it and tell me
> the peak level.

A typical run resolves the oscillator with `search_node_classes` and
`resolve_node_class`, builds with `create_metasound_source`, `add_node`,
`add_graph_input` and the connect tools, plays it through
`start_audition` and `set_audition_param`, then proves it with
`start_source_render`, `finish_source_render` and `analyze_rendered_wav`.
Ask it to save, and `build_to_asset` writes the asset you named.

## Good to know

- **Save your work before an agent session.** An agent can still crash the
  editor (an extreme value on an engine node is one known way), and a
  crash loses unsaved work. [SECURITY.md](SECURITY.md) lists the known
  cases.
- **An unfocused editor mutes itself, and the mute reaches renders.** A
  render made while the editor is in the background can come out silent.
  Keep the editor focused while rendering, or enable **Editor Preferences
  > Miscellaneous > Sound > Allow Background Audio** (off by default, set
  per project). "Use Less CPU when in Background" still slows an unfocused
  editor, which coarsens render lengths.
- **Renders are kept.** WAVs land in your project's
  `Saved/MetaSoundToolset/Renders` folder and are never pruned.
- **Saved graphs cannot be fully read back.** The 5.8 Builder API cannot
  list the nodes inside a MetaSound it did not build, so
  `describe_metasound` reports a saved asset's inputs, outputs and type.
  Graphs built in the current session read back in full, wiring included.
- **Overwrites are guarded, not undoable.** Replacing an asset works only
  for MetaSounds, only with `overwrite=true`, and is refused while
  anything this toolset can see still references the target. Keep your
  project in version control.
- **On a source-controlled project**, saving may open a check-out dialog.
  That expectation comes from reading engine source and has not been
  tested here.
- **Generated presets open locked** in the MetaSound editor. That is how
  MetaSound presets work: inputs are editable and the graph stays
  read-only until you convert it.

## Tests

The plugin ships its test suite: 246 tests covering every tool on its
success path and on its refusals. Run it headless:

```bash
UnrealEditor-Cmd.exe <YourProject>.uproject -ExecCmds="Automation RunTests AI.MetaSoundToolset;quit" -Unattended -NullRHI
```

The tests create and remove files under `Saved/` and a temporary content
mount. How this release was verified, and on what, is in
[VERIFICATION.md](VERIFICATION.md).

## Security

Epic's MCP server has no authentication and is meant for your own machine
only. Any process on your machine can call every tool, and tool results,
asset names and file paths included, go to whichever model your client
uses. [SECURITY.md](SECURITY.md) covers what these tools can and cannot
reach, what stays your responsibility, and how to check that the tool
descriptions an agent reads have not changed
(`tools/verify_tool_surface.py`).

## License

MIT © 2026 Dean Cristofori
([Nexus-nimdA](https://github.com/Nexus-nimdA)). See [LICENSE](LICENSE).
