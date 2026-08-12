"""What a Unity build calls itself, read off the build.

Every Unity player ships a ``<productName>_Data`` folder, and inside it an
``app.info`` holding exactly two lines -- the project's ``companyName`` then its
``productName``, as the editor had them when the build was made. That is the
project's own internal identity: renaming the install folder, repacking it or
shipping several players side by side (a game, its VR build and its studio) does
not change it.

Read as a SET, because one install routinely is several players:
``Koikatu_Data``, ``KoikatuVR_Data`` and ``CharaStudio_Data`` sit in the same
folder and name three products of one project.

Stdlib only, no bridge session: this answers "which game is this folder" before
anything has been loaded.
"""

from __future__ import annotations

import os

_DATA_SUFFIX = "_Data"
_APP_INFO = "app.info"


def data_folders(game_root):
    """Every ``*_Data`` folder directly under an install root."""
    try:
        entries = os.listdir(game_root)
    except OSError:
        return []
    found = []
    for name in entries:
        if not name.endswith(_DATA_SUFFIX):
            continue
        path = os.path.join(game_root, name)
        if os.path.isdir(path):
            found.append(path)
    return sorted(found)


def names(game_root):
    """The lowercased identities this install answers to: every player's
    ``companyName`` and ``productName``, plus each ``*_Data`` folder's own stem
    for a build that ships no ``app.info``."""
    identities = set()
    for folder in data_folders(game_root):
        identities.add(os.path.basename(folder)[: -len(_DATA_SUFFIX)].lower())
        try:
            with open(os.path.join(folder, _APP_INFO), "r", encoding="utf-8",
                      errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines[:2]:
            line = line.strip()
            if line:
                identities.add(line.lower())
    return identities
