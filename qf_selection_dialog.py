"""Kodi dialog for manual station assignment (WindowXMLDialog).

Opened by service.py in a dedicated thread (never from the request loop); the outcome is
read from `result` after doModal() returns. Needs resources/skins/default/1080i/qf-station-select.xml.
"""

import xbmc
import xbmcaddon
import xbmcgui

XML_FILE = "qf-station-select.xml"

CTRL_TITLE = 10
CTRL_TEXT = 11
CTRL_LIST = 100
CTRL_SAVE = 200
CTRL_CANCEL = 300
CTRL_OK = 301

ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92

STR_TITLE = 32100
STR_TEXT = 32101
STR_SAVE = 32102
STR_CANCEL = 32103
STR_OK = 32104

MARK = "> "  # ASCII: skin fonts may lack symbol glyphs


def _candidate_label2(candidate):
    parts = []
    for key in ("country", "channel_key"):
        value = str(candidate.get(key) or "").strip()
        if value:
            parts.append(value)
    return " \u00b7 ".join(parts)


class StationSelectDialog(xbmcgui.WindowXMLDialog):
    """Usage: dlg = StationSelectDialog(XML_FILE, path, "default", "1080i"); dlg.configure(...); dlg.doModal()"""

    def configure(self, station_name, candidates):
        self._station_name = str(station_name or "")
        self._candidates = list(candidates or [])
        self._marked = 0
        self._save = True  # default: remember the choice
        self.result = None  # {"stationuuid": str, "save": bool} on "Uebernehmen"
        self._addon = xbmcaddon.Addon()

    def _s(self, string_id):
        return self._addon.getLocalizedString(string_id)

    def _item_label(self, index):
        name = str(self._candidates[index].get("name") or "?")
        return (MARK if index == self._marked else "") + name

    def _refresh_labels(self):
        for index in range(len(self._candidates)):
            self.getControl(CTRL_LIST).getListItem(index).setLabel(self._item_label(index))

    def _refresh_save_label(self):
        # Plain button + text prefix instead of a radiobutton: skin-independent rendering.
        self.getControl(CTRL_SAVE).setLabel(("[x] " if self._save else "[ ] ") + self._s(STR_SAVE))

    def onInit(self):
        self.getControl(CTRL_TITLE).setLabel(self._s(STR_TITLE))
        self.getControl(CTRL_TEXT).setText(self._s(STR_TEXT).replace("%s", self._station_name))
        self.getControl(CTRL_CANCEL).setLabel(self._s(STR_CANCEL))
        self.getControl(CTRL_OK).setLabel(self._s(STR_OK))
        self._refresh_save_label()

        listing = self.getControl(CTRL_LIST)
        listing.reset()
        items = []
        for index, candidate in enumerate(self._candidates):
            item = xbmcgui.ListItem(self._item_label(index), _candidate_label2(candidate))
            items.append(item)
        listing.addItems(items)
        self.setFocusId(CTRL_LIST)

    def onClick(self, control_id):
        if control_id == CTRL_LIST:
            self._marked = self.getControl(CTRL_LIST).getSelectedPosition()
            self._refresh_labels()
        elif control_id == CTRL_SAVE:
            self._save = not self._save
            self._refresh_save_label()
        elif control_id == CTRL_OK:
            if 0 <= self._marked < len(self._candidates):
                self.result = {
                    "stationuuid": str(self._candidates[self._marked].get("stationuuid") or ""),
                    "save": bool(self._save),
                }
            self.close()
        elif control_id == CTRL_CANCEL:
            self.close()

    def onAction(self, action):
        if action.getId() in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self.close()
            return
        super().onAction(action)


def open_dialog(addon_path, station_name, candidates):
    """Create (not open) the dialog. Caller runs doModal() in a worker thread and reads dialog.result."""
    dialog = StationSelectDialog(XML_FILE, addon_path, "default", "1080i")
    dialog.configure(station_name, candidates)
    return dialog
