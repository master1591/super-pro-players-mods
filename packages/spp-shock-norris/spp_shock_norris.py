"""Free Shock Norris character for the verified SPP package loader.

Uses assets already installed with BombSquad; never rewrites game files,
unlocks Bones, patches the engine, or takes over the music player.
The SPP server supplies the networked red effects and /shock selection.
"""

from __future__ import print_function


CHARACTER_NAME = "Shock Norris"
PACKAGE_VERSION = "0.1.0-beta"
OWNER_MARKER = "spp-shock-norris"
DEFAULT_COLOR = (0.55, 0.08, 0.05)
DEFAULT_HIGHLIGHT = (0.75, 0.18, 0.10)
APPEARANCE_FIELDS = (
    "color_texture", "color_mask_texture", "icon_texture",
    "icon_mask_texture", "head_mesh", "torso_mesh", "pelvis_mesh",
    "upper_arm_mesh", "forearm_mesh", "hand_mesh", "upper_leg_mesh",
    "lower_leg_mesh", "toes_mesh", "style",
)
SOUNDS = {
    "jump_sounds": ("shieldHit",),
    "attack_sounds": ("shieldHit",),
    "impact_sounds": ("shieldHit",),
    "death_sounds": ("shieldDown",),
    "pickup_sounds": ("powerup01",),
    "fall_sounds": ("shieldDown",),
}


def register_appearance(registry, appearance_type):
    """Register only our own alias, preserving installed native asset handles."""
    existing = registry.get(CHARACTER_NAME)
    if existing is not None:
        if getattr(existing, "_spp_shock_norris_owner", None) != OWNER_MARKER:
            raise RuntimeError("Another mod already registered Shock Norris.")
        return existing
    bones = registry.get("Bones")
    if bones is None:
        raise RuntimeError("BombSquad's native skeleton appearance is unavailable.")

    # Validate before Appearance's constructor adds an object to the registry.
    # Using the original values also supports new engine asset-handle objects.
    fields = {name: getattr(bones, name) for name in APPEARANCE_FIELDS}
    appearance = appearance_type(CHARACTER_NAME)
    try:
        for name, value in fields.items():
            setattr(appearance, name, value)
        # Bones' own mask leaves most of the skeleton ivory. A native white
        # mask makes this alias fully tintable without replacing any asset.
        appearance.color_mask_texture = "white"
        for name, values in SOUNDS.items():
            setattr(appearance, name, list(values))
        appearance.default_color = DEFAULT_COLOR
        appearance.default_highlight = DEFAULT_HIGHLIGHT
        appearance._spp_shock_norris_owner = OWNER_MARKER
        appearance._spp_shock_norris_version = PACKAGE_VERSION
    except Exception:
        if registry.get(CHARACTER_NAME) is appearance:
            del registry[CHARACTER_NAME]
        raise
    return appearance


def start(context):
    """Manager ABI: registering the character needs no polling or downloads."""
    if context.get("api_version") != 9:
        raise RuntimeError("Shock Norris requires BombSquad plugin API 9.")
    import babase
    from bascenev1lib.actor.spazappearance import Appearance

    classic = babase.app.classic
    if classic is None:
        raise RuntimeError("BombSquad Classic is not ready.")
    register_appearance(classic.spaz_appearances, Appearance)
    print("[SPP Shock Norris] Ready: choose Shock Norris in a player profile "
          "or type /shock on SPP. Free cosmetic; normal combat stats.")
