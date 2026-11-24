"""Station assignment management, started from the ASM-QF settings (RunScript).

Runs in its own interpreter, so it never touches service state directly: "assign" and cache
invalidation go through a Window property that the service polls; mappings are read/deleted
in the shared sqlite DB.
"""

import os
import sys

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
ROOT = ADDON.getAddonInfo("path")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import station_mapping as sm  # noqa: E402
from app.station_identity import build_station_key  # noqa: E402

WINDOW = xbmcgui.Window(10000)
MANUAL_CMD = "RadioMonitor.QF.Manual.Command"
REQ_STATION = "RadioMonitor.QF.Request.Station"

STR_NO_STATION = 32114
STR_DELETED = 32117
STR_ASK_DELETE = 32118
STR_NONE_SAVED = 32119
STR_SHOW = 32120
STR_NO_MAPPING = 32121
STR_SRC_LEGACY = 32122
STR_SRC_USER = 32123
STR_SRC_TECH = 32124
STR_MANAGE_TITLE = 32113


def _s(string_id):
    return ADDON.getLocalizedString(string_id)


def _db_path():
    return xbmcvfs.translatePath(f"special://userdata/addon_data/{ADDON_ID}/song_data.db")


def _source_text(mapping):
    return {
        sm.CONFIRM_USER: _s(STR_SRC_USER),
        sm.CONFIRM_TECHNICAL: _s(STR_SRC_TECH),
    }.get(mapping.get("confirmation_source"), _s(STR_SRC_LEGACY))


def assign():
    if not (WINDOW.getProperty(REQ_STATION) or "").strip():
        xbmcgui.Dialog().notification("ASM-QF", _s(STR_NO_STATION), xbmcgui.NOTIFICATION_INFO, 4000)
        return
    WINDOW.setProperty(MANUAL_CMD, "assign")


def show():
    station = (WINDOW.getProperty(REQ_STATION) or "").strip()
    if not station:
        xbmcgui.Dialog().notification("ASM-QF", _s(STR_NO_STATION), xbmcgui.NOTIFICATION_INFO, 4000)
        return
    mapping = sm.load_mapping(_db_path(), "name", build_station_key(station))
    if mapping:
        value = f"{mapping.get('station_name') or mapping.get('stationuuid')} ({_source_text(mapping)})"
    else:
        value = _s(STR_NO_MAPPING)
    xbmcgui.Dialog().ok("ASM-QF", _s(STR_SHOW).replace("%s", station, 1).replace("%s", value, 1))


def manage():
    dialog = xbmcgui.Dialog()
    while True:
        mappings = sm.list_mappings(_db_path())
        if not mappings:
            dialog.ok("ASM-QF", _s(STR_NONE_SAVED))
            return
        labels = [
            f"{m['source_identifier'].split(':', 1)[-1]}  \u2192  {m.get('station_name') or m['stationuuid'][:8]}"
            f"  [{_source_text(m)}]"
            for m in mappings
        ]
        index = dialog.select(_s(STR_MANAGE_TITLE), labels)
        if index < 0:
            return
        chosen = mappings[index]
        if dialog.yesno("ASM-QF", _s(STR_ASK_DELETE) + "\n" + labels[index]):
            sm.delete_mapping(_db_path(), chosen["source_type"], chosen["source_identifier"])
            WINDOW.setProperty(MANUAL_CMD, "invalidate:" + chosen["source_identifier"])
            dialog.notification("ASM-QF", _s(STR_DELETED), xbmcgui.NOTIFICATION_INFO, 3000)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "manage"
    {"assign": assign, "show": show, "manage": manage}.get(mode, manage)()
