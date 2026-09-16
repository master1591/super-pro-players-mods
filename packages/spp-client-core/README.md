# SPP client core package

This manager-loaded package watches BombSquad's reliable local chat history for
strict `SPP1` victory events. It maps only the six allowed `(team, stage)` pairs
to locally verified files in `spp-victory-audio`; server messages can never
supply a path, URL, or Python command.

Initial playback targets are Android and Windows on BombSquad API 9. Android
uses Ballistica's built-in OS music bridge. Windows uses the operating system's
MP3 decoder through `winmm.dll`; no executable dependency is downloaded.

The transport is intentionally one visible, concise chat line per round. A
future native protocol or authenticated side channel can replace it without
changing the catalog mapping.
