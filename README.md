# SUPER PRO PLAYERS Mods Manager

A one-command installer and updater for official SUPER PRO PLAYERS BombSquad client mods.

> **Beta:** The manager is ready, but the public gameplay-package catalog is empty for now. Victory music and other SPP packages will be published only after testing.

## Quick install (Windows and Android)

You do **not** need to download this file manually or search for BombSquad's mods folder.

1. Open BombSquad.
2. Open **Settings → Advanced**.
3. Enable **Developer Console Button**.
4. Open the developer console and select **Python**.
5. Paste the complete command below and press **Exec**:

```python
import urllib.request as u,hashlib as h;d=u.urlopen('https://raw.githubusercontent.com/master1591/super-pro-players-mods/17e78cf4a7eed991c8e1db4bed62abf62eea7920/install.py',timeout=20).read(131073);len(d)<=131072 or (_ for _ in ()).throw(RuntimeError('SPP installer is too large'));h.sha256(d).hexdigest()=='2b7a0c6f088426a1f9e2fbc47fc965f9051b6ab73543fcf1c4fed2f960570161' or (_ for _ in ()).throw(RuntimeError('SPP installer security check failed'));exec(compile(d,'spp_installer','exec'),{'__name__':'__main__'})
```

6. Wait for `Installed successfully. Fully restart BombSquad once.`
7. Fully close BombSquad and open it again.

Use only the command in this repository or the official SUPER PRO PLAYERS Discord. Do not use installer commands copied from unknown people.

### Release integrity

| File | Immutable commit | SHA-256 |
|---|---|---|
| `install.py` | `17e78cf4a7eed991c8e1db4bed62abf62eea7920` | `2b7a0c6f088426a1f9e2fbc47fc965f9051b6ab73543fcf1c4fed2f960570161` |
| `super_pro_players_mod_manager.py` | `d72e128845f4275cfa6db458447c2f98b40cd8e4` | `c4260c88a298f26162a8721a6f60c1226d1be9508dbc161599482fee315480f8` |

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

## Current limitation

The public package catalog is intentionally empty. Installing the manager does **not** install victory music or other gameplay packages yet. A `No packages available` result is expected until the first package is published.

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
