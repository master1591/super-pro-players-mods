# SUPER PRO PLAYERS Mods Manager

A one-command installer and updater for official SUPER PRO PLAYERS BombSquad client mods.

> **Beta:** The first official package release adds synchronized SUPER and PRO
> victory music for supported BombSquad API 9 builds on Windows and Android.

## Quick install (supported BombSquad builds)

You do **not** need to download this file manually or search for BombSquad's mods folder.
The manager installer supports BombSquad plugin APIs 6–9 on desktop and mobile,
including macOS. Individual packages can have narrower platform requirements;
the current victory-music package supports Windows and Android only.

1. Open BombSquad.
2. Open **Settings → Advanced**.
3. Enable **Developer Console Button**.
4. Open the developer console and select **Python**.
5. Paste the complete command below and press **Exec**:

```python
import urllib.request as u,hashlib as h;d=u.urlopen('https://raw.githubusercontent.com/master1591/super-pro-players-mods/7cdab8770d9ef822a8d2ecc3f0e1d9634f854d00/install.py',timeout=20).read(131073);len(d)<=131072 or (x for x in ()).throw(RuntimeError('SPP installer is too large'));h.sha256(d).hexdigest()=='6372c6f5ad65099aa913fd810ce5a65658c21f3289988c6136f9f65a585e395c' or (x for x in ()).throw(RuntimeError('SPP installer security check failed'));n=chr(95)+chr(95);exec(compile(d,'spp-installer','exec'),{n+'name'+n:n+'main'+n})
```

The command intentionally contains no underscore characters, so Discord cannot
remove parts of it as Markdown. Keep it inside a fenced code block when sharing.

6. Wait for `Installed successfully. Fully restart BombSquad once.`
7. Fully close BombSquad and open it again.

Use only the command in this repository or the official SUPER PRO PLAYERS Discord. Do not use installer commands copied from unknown people.

### Release integrity

| File | Immutable commit | SHA-256 |
|---|---|---|
| `install.py` | `7cdab8770d9ef822a8d2ecc3f0e1d9634f854d00` | `6372c6f5ad65099aa913fd810ce5a65658c21f3289988c6136f9f65a585e395c` |
| `super_pro_players_mod_manager.py` | `ff8639cebd191b28d21eb0420df00503395750df` | `c6e25aad390a99fe541f38dbc37443848c93fef9e5b06f839a45756af3606194` |

## Open the manager

On newer BombSquad versions:

**Settings → Advanced → Plugins → SPPModManagerLoader → Settings**

On API 6 and early API 7 versions, the manager opens automatically after BombSquad starts.

## What the installer does

The installer runs inside BombSquad and automatically:

- Detects the BombSquad plugin API.
- Locates BombSquad's own user mods directory.
- Downloads the pinned official manager file over HTTPS.
- Verifies its exact SHA-256 digest and Python syntax.
- Installs it atomically and creates the correct API-specific loader.
- Enables the loader without touching unrelated mods.
- Restores the previous manager automatically if activation fails.

## Supported versions

| BombSquad version | Plugin API | Support |
|---|---:|---|
| 1.5.23–1.7.1 | 6 | Best-effort legacy support |
| 1.7.2–1.7.19 | 7 | Best-effort legacy support |
| 1.7.20–1.7.36 | 8 | Best-effort support |
| 1.7.37+ and current 1.8.x builds | 9 | Primary target |

Versions older than 1.5.23 are unsupported because they predate the normal plugin system. Future API 10+ builds will require an updated manager. After changing to a BombSquad release with a different plugin API, run the official install command again and restart the game.

## Security

- Both the public installer command and the installer itself pin exact SHA-256 digests.
- The installer uses immutable GitHub commit URLs and rejects redirects.
- Package archives and every extracted file are SHA-256 verified.
- The manager rejects path traversal, symlinks, unexpected files, executable file types, oversized archives, and dangerous compression ratios.
- Downloads are staged before activation, with a known-working rollback copy.
- Executable package updates require a second confirmation in the manager.
- No BombSquad, Discord, or GitHub passwords or tokens are collected or stored.

SHA-256 protects against corruption or unexpected changes. It is not a publisher signature if the official repository itself is compromised.

## Victory-music package

Open the manager, press **CHECK**, and then press **INSTALL / UPDATE ALL**. The
manager downloads and verifies both the SPP client core and victory-audio
package. Fully close and reopen BombSquad after installation.

The victory package currently targets BombSquad API 9 builds 22796 and newer
on Windows and Android. The manager itself continues to support APIs 6-9, but
older APIs will show the victory packages as incompatible instead of forcing
an unsafe installation.

SUPER and PRO each use the same approximately 10-second teaser for both 1/3
and 2/3. At 3/3, the winning team's dance section plays for approximately
20 seconds. Cues begin when the native score or final winner text is revealed;
they stop when the score screen exits, even if a player continues early.
Music volume and the victory-music toggle remain in the manager settings.

Client core **0.1.2-beta** and audio **0.1.1-beta** align victory music with
the score-screen reveals and keep the default soundtrack silent on SPP.
Existing users only need **CHECK → INSTALL / UPDATE ALL**, confirm the code
update if asked, and fully restart BombSquad. Manager **0.1.2-beta** keeps its
one-shot boot-health timer alive so a successful package update is not rolled
back and offered repeatedly. Both package versions above must be installed for
the new cuts.

After joining the updated SPP server, look for **SPP music connected**. This
means the server acknowledged this client's registration; it is not an audio
hardware test. Discovery no longer depends on the server's displayed name.
Short discovery messages may appear in chat before registration. Only the six
verified local clips can be selected by the server. BombSquad's normal music
stays silent throughout the recognized SPP connection, including gameplay,
between clips, and when Victory music is switched off. Sound effects stay on.
Normal music and its saved volume preference return after leaving the server.

Windows prepares a bounded set of silent audio decoders on a dedicated worker
thread, so loading a clip does not block BombSquad's game thread. Timers follow
actual playback start, and stopped or obsolete requests cannot play later.
Android retains the game's built-in asynchronous OS music bridge.

The Windows and Android playback paths are covered by isolated tests, but
actual speaker output must still be checked on both devices after updating.

### Installed but victory music is silent

1. Confirm the manager lists `spp-client-core: 0.1.2-beta` and
   `spp-victory-audio: 0.1.1-beta`, with Victory music enabled and volume above zero.
2. Fully close and reopen BombSquad, then rejoin SPP and wait for
   **SPP music connected**. If it never appears, reconnect once and send staff
   the developer-console lines beginning `[SPP Client]`.
3. The server owner should confirm a successful Witchly upload before restarting,
   then look for `SUPER PRO READY version=2026.09.17.1`. Registration and round
   event diagnostics begin `SUPER PRO MUSIC`.
4. If connected but silent, report which device, team and round (1/3, 2/3, 3/3)
   failed, plus any playback warning. Do not reinstall unrelated mods or delete
   your game settings to troubleshoot this feature.

## Troubleshooting

### Nothing appears after pressing Exec

Wait a few seconds and check the developer-console history. Send SPP staff a screenshot containing the complete error.

### `Locale is not JSON serializable`

Old versions of the community Translate mod stored invalid `Locale` objects in BombSquad's shared settings. This manager narrowly repairs its known language keys. If another invalid key remains, update or temporarily disable the old mod, restart BombSquad, and run the installer again.

### Download or connection error

Check that the device has Internet access, its date and time are correct, and GitHub is reachable. Do not disable the security check or use an unofficial mirror.

### Manager does not appear after installation

Fully close BombSquad instead of only returning to its main menu, then reopen it. Check **Settings → Advanced → Plugins → SPPModManagerLoader**.

### Unsupported API

The detected BombSquad API is outside APIs 6–9. Do not force installation; wait for an updated manager.

## Reinstall or update

Run the newest official command again and fully restart BombSquad. Existing package state and rollback data are preserved.

## Uninstall

Fully close BombSquad and remove only these files from its user mods folder:

```text
super_pro_players_mod_manager.py
spp_mod_manager_loader.py
```

To erase downloaded SPP packages and settings too, also remove `spp_mod_manager_data`. Do not delete the complete mods folder because it may contain unrelated plugins.

## License

Released under the [MIT License](LICENSE).
