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

``product`` picks the ONE of those the install is, which is what a browser tab
is named after.

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


def players(game_root):
    """Every player in this install as ``(company, product)``, in ``*_Data`` folder
    order. A build with no ``app.info`` still answers with its folder stem as the
    product and an empty company -- that stem is the productName the Unity build
    pipeline named the folder after."""
    found = []
    for folder in data_folders(game_root):
        stem = os.path.basename(folder)[: -len(_DATA_SUFFIX)]
        try:
            with open(os.path.join(folder, _APP_INFO), "r", encoding="utf-8",
                      errors="replace") as handle:
                lines = [line.strip() for line in handle.read().splitlines()]
        except OSError:
            lines = []
        company = lines[0] if len(lines) > 0 else ""
        product = lines[1] if len(lines) > 1 and lines[1] else stem
        found.append((company, product))
    return found


def names(game_root):
    """The lowercased identities this install answers to: every player's
    ``companyName`` and ``productName``, plus each ``*_Data`` folder's own stem
    for a build that ships no ``app.info``."""
    identities = set()
    for folder in data_folders(game_root):
        identities.add(os.path.basename(folder)[: -len(_DATA_SUFFIX)].lower())
    for company, product in players(game_root):
        for identity in (company, product):
            if identity:
                identities.add(identity.lower())
    return identities


def _named_by_company(candidates):
    """The product one of the install's own ``companyName`` values names.

    An Illusion project states its identity twice: ``Koikatu_Data`` carries
    company ``illusion__Koikatu`` / product ``Koikatu``, while the studio player
    beside it carries the same company and product ``CharaStudio``. So the
    product the company field ENDS WITH is the project, and the other players are
    its extra builds. Compared on alphanumerics only, because that separator is
    spelled ``__`` in one folder and ``_`` in the next."""
    def squashed(text):
        return "".join(char for char in text.lower() if char.isalnum())

    companies = [squashed(company) for company, _product in candidates if company]
    for _company, product in candidates:
        key = squashed(product)
        if key and any(company.endswith(key) for company in companies):
            return product
    return ""


def _prefix_owner(candidates):
    """The product the most other products extend -- ``KoikatsuSunshine`` owns
    ``KoikatsuSunshine_VR``. A project's extra players are named after it, so the
    one being extended is the project itself. Ties break on the shorter name,
    then on the name, so this never depends on directory order."""
    products = [product for _company, product in candidates]
    ranked = sorted(
        products,
        key=lambda product: (-sum(1 for other in products
                                  if other != product and other.startswith(product)),
                             len(product), product))
    return ranked[0] if ranked else ""


def product(game_root):
    """The ONE product name this install is, or "" when the folder is no Unity
    install at all. Every player it ships is a candidate (see ``players``); which
    of them is the project is decided by the install's own fields, never by a
    list of known games."""
    candidates = players(game_root)
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0][1]
    return _named_by_company(candidates) or _prefix_owner(candidates)
