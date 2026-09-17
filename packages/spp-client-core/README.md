# SPP client core package

This manager-loaded package watches BombSquad's reliable local chat history for
strict `SPP1` victory events. It maps only the six allowed `(team, stage)` pairs
to locally verified files in `spp-victory-audio`; server messages can never
supply a path, URL, or Python command.

Playback targets are Android and Windows on BombSquad API 9. Android
uses Ballistica's built-in OS music bridge. Windows uses the operating system's
MP3 decoder through `winmm.dll`; no executable dependency is downloaded.
Windows decoder opens and playback commands run on one bounded worker, with
silent preloading, stale-request cancellation and app-thread completion polling.

The server emits cues at the native score/winner reveal. A STOP frame must
match the current server session, client nonce and active event before it can
end playback on score-screen exit. Original BombSquad music is suppressed
throughout a recognized SPP connection, separately from the user-controlled
victory-music toggle. Its latest requested soundtrack is restored on leaving.

The transport uses visible, strictly parsed chat frames for registration,
victory cues and score-screen exit. A
future native protocol or authenticated side channel can replace it without
changing the catalog mapping.
