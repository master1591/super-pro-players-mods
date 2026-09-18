# Shock Norris

A free electrical-themed skeleton character for SUPER PRO PLAYERS.

## Existing Mod Manager users

With manager **0.1.2-beta**, let its automatic check finish (or press **CHECK**), then
press **INSTALL / UPDATE ALL** and confirm if asked. Fully close and reopen
BombSquad once. No new installer command, manual file copying, or game reinstall.
Older managers retain their existing second-click confirmation for Python code.
If you still have manager 0.1.0/0.1.1 or see updates roll back repeatedly, run
the current official installer once first; this package cannot replace the
manager bootstrap itself.

Choose **Shock Norris** in **Settings → Player Profiles → your profile →
Character**, then join SPP. Alternatively, type **/shock** on SPP to enable him
without editing a profile. The server also makes this command available to
players without the pack. The command takes effect on your **next spawn or
round** and never kills or respawns you. Use **/shock off** to return to your
profile's normal appearance next spawn (Spaz if that profile is Shock Norris).

There is no price, rank, subscription, or Bones purchase requirement. The new
appearance does not unlock or change the original Bones character. Shock Norris
uses normal native skeleton physics, with no extra health, speed, or punch
power. Team membership, scores, and purchased effects remain unchanged.

## What the first version looks and sounds like

The SPP server supplies a red skeleton with restrained pulsing red flashes,
a small glow, a quiet charge-up sound, and native electrical hit/discharge
sounds. Other players see and hear the same server-created effects even if
they have not installed this package. It approximates the red-lightning concept;
it does not render a transparent body made entirely of branching lightning.

All meshes, textures, and sounds are loaded by name from the player's existing
game. No proprietary assets are redistributed and no packaged game files are
modified. The character uses native sound effects, never the victory music
player. Supported package target: API 9, build 22796 or newer; actual device
appearance and performance must be checked on each platform before release.

## Server requirement

SPP must be running the matching server update for its red effects and the
`/shock` command. Installing the client pack alone adds the profile appearance
and electrical character sounds for local games. It cannot add features to an
unmodified remote server. Other servers may fall back to Spaz for an unknown
profile character; select a built-in character if that server rejects it.
