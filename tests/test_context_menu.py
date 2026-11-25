import ctypes
import ctypes.util
import importlib.util
from pathlib import Path
import tkinter as tk
import time
import tempfile

path = Path(__file__).resolve().parents[1] / "source/package/usr/lib/packet-desk/packet_desk.py"
spec = importlib.util.spec_from_file_location("packet_desk", path)
packet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packet)

configuration = tempfile.TemporaryDirectory()
packet.profile_file = lambda: Path(configuration.name) / "profiles.json"
root = tk.Tk()
root.geometry("400x200+25+25")
app = packet.Application(root, [])
app.open_editor()
editor = app.current_session().editor
editor.window.geometry("660x450+460+25")
widget = editor.text
menu = next(child for child in widget.winfo_children() if isinstance(child, tk.Menu))
widget.insert("1.0", "VERSION\nMYCALL AF0WX\n")
widget.edit_reset()
widget.edit_modified(False)
root.update()

def settle():
    for _ in range(10):
        root.update()
        time.sleep(.02)

settle()

X11 = ctypes.CDLL(ctypes.util.find_library("X11"))
Xtst = ctypes.CDLL(ctypes.util.find_library("Xtst"))
X11.XOpenDisplay.argtypes = [ctypes.c_char_p]
X11.XOpenDisplay.restype = ctypes.c_void_p
X11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
Xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
Xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
display = X11.XOpenDisplay(None)
assert display

def click(button, x, y):
    Xtst.XTestFakeMotionEvent(display, -1, x, y, 0)
    for pressed in (True, False):
        Xtst.XTestFakeButtonEvent(display, button, pressed, 0)
        X11.XSync(display, False)
        settle()

def open_menu():
    editor.window.lift()
    widget.focus_force()
    settle()
    click(3, widget.winfo_rootx() + 150, widget.winfo_rooty() + 40)
    assert menu.winfo_ismapped(), "Right-click must open the menu"
    assert root.grab_current() is menu
    assert menu.grab_status() == "local"

def assert_menu_closed():
    assert not menu.winfo_ismapped()
    assert root.grab_current() is None

widget.tag_add("sel", "1.0", "2.0")
root.clipboard_clear()
root.clipboard_append("keep this clipboard text")
open_menu()
assert menu.entrycget("Delete selection", "state") == "normal"
click(1, menu.winfo_rootx() + 30, menu.winfo_rooty() + menu.yposition("Delete selection") + 6)
assert_menu_closed()
assert widget.get("1.0", "end-1c") == "MYCALL AF0WX\n"
assert widget.edit_modified()
assert root.clipboard_get() == "keep this clipboard text"
widget.edit_undo()
assert widget.get("1.0", "end-1c") == "VERSION\nMYCALL AF0WX\n"

widget.tag_remove("sel", "1.0", "end")
open_menu()
assert menu.entrycget("Delete selection", "state") == "disabled"
click(1, root.winfo_rootx() + 40, root.winfo_rooty() + 20)
assert_menu_closed()

open_menu()
menu.event_generate("<Escape>")
root.update()
assert_menu_closed()

open_menu()
editor.window.withdraw()
root.update()
assert_menu_closed()
editor.window.deiconify()
root.update()
open_menu()
editor.window.iconify()
settle()
assert editor.window.state() == 'iconic'
assert_menu_closed()
editor.window.deiconify()
root.update()
open_menu()
# A separate Tk application stands in for another desktop window.
other = tk.Tk()
other.geometry("210x40+30+630")
clicked = []
target = tk.Button(other, text="Another application's button", command=lambda: clicked.append(True))
target.pack(fill="both", expand=True)
settle()
open_menu()
click(1, target.winfo_rootx() + 45, target.winfo_rooty() + 18)
assert clicked, "The open menu must allow another application's button to receive clicks"
assert_menu_closed()
other.destroy()

open_menu()
editor.window.destroy()
root.update()
assert root.grab_current() is None

transcript = app.current_session().terminal
readonly_menu = next(child for child in transcript.winfo_children() if isinstance(child, tk.Menu))
assert "Delete selection" not in [readonly_menu.entrycget(index, "label")
                                  for index in range(readonly_menu.index("end") + 1)]
root.destroy()
print("Right-click, Delete selection, Undo, outside-click, Escape, window hide, minimize, app switching and destroy passed.")
