import importlib.util
import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import time
import tkinter as tk

source = Path(__file__).resolve().parents[1] / "source/package/usr/lib/packet-desk/packet_desk.py"
spec = importlib.util.spec_from_file_location("packet_desk", source)
packet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packet)

commands = packet.parse_command_file(
    '# Batch\r\n\tLT\t1 "data"\rLT 2 literal (text)\nBLT 1 EVERY 00:08:00\n'
    'TXDELAY 30/30\nXOFF $13\n8BITCONV ON'
)
assert [number for number, _, _ in commands] == [2, 3, 4, 5, 6, 7]
assert commands[0][2] == b'LT 1 "data"\r'
assert commands[1][2] == b"LT 2 literal (text)\r"
try:
    packet.parse_command_file("VERSION\n1 EVERY 00:08:00/EVERY 00:00:00\n"
                              "A/1 Link state is: DISCONNECTED\nTXDELAY 30/30 (300/300 msec)\n"
                              "XON $11 (CTRL-Q)\ncmd:")
except packet.CommandFileError as error:
    assert error.line_number == 2
    for expected in ("Add BLT", "Remove the link status", "TXDELAY 30/30.", "XON $11.", "Remove the prompt"):
        assert expected in str(error), error
else:
    raise AssertionError("Raw DISPLAY output unexpectedly passed the check")

def until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Expected GUI state not reached")

def receive(expected):
    until(lambda: select.select([master], [], [], 0)[0])
    assert os.read(master, 4096) == expected

with tempfile.TemporaryDirectory() as temporary:
    config = Path(temporary)
    packet.profile_file = lambda: config / "profiles.json"
    (config / "appearance.json").write_text('"Dark"')
    root = tk.Tk()
    master, slave = pty.openpty()
    app = packet.Application(root, [os.ttyname(slave)])
    session = app.current_session()
    appearance = app.appearance
    assert appearance.selected.get() == "Dark"
    assert session.terminal.cget("background") == "#1a1a1a"
    app.new_session()
    app.notebook.select(0)
    app.open_editor()
    editor = session.editor
    root.update()
    editor.text.insert("1.0", "VERSION\nMYCALL\n")
    editor.text.edit_reset()
    editor.write(config / "commands.txt")
    menu_path = editor.window.cget("menu")
    edit_menu = editor.window.nametowidget(editor.window.tk.call(menu_path, "entrycget", "Edit", "-menu"))
    editor.text.tag_add("sel", "1.0", "2.0")
    edit_menu.invoke("Delete selection")
    assert editor.text.get("1.0", "end-1c") == "MYCALL\n"
    editor.text.edit_undo()
    assert editor.text.get("1.0", "end-1c") == "VERSION\nMYCALL\n"
    edit_menu.invoke("Clear editor")
    assert editor.text.get("1.0", "end-1c") == ""
    assert (config / "commands.txt").read_text() == "VERSION\nMYCALL\n"
    editor.text.edit_undo()
    assert editor.text.get("1.0", "end-1c") == "VERSION\nMYCALL\n"
    editor.window.withdraw()

    appearance.shaded.set(True)
    appearance.save()
    root.update()
    element = appearance.style.layout("TNotebook.Tab")[0][0]
    assert element == "PacketDesk.Notebook.tab"
    images = appearance.tab_images[appearance.style.theme_use()]
    assert images[0].get(12, 3) != images[0].get(12, 20)
    appearance.customize()
    root.update()
    color_editor = appearance.color_editor
    assert color_editor.values["tab_selected"].get() == "#b8131b"
    for name, color in (("terminal", "#101820"), ("editor", "#192630"),
                        ("sent", "#abcdef"), ("tab_selected", "#335577")):
        color_editor.values[name].set(color)
    color_editor.save()
    root.update()
    assert appearance.selected.get() == "Custom"
    assert session.terminal.cget("background") == "#101820"
    assert session.terminal.tag_cget("sent", "foreground") == "#abcdef"
    assert editor.text.cget("background") == "#192630"
    assert appearance.style.lookup("TNotebook.Tab", "background", ("selected",)) == "#335577"
    restored = packet.Appearance(root)
    assert restored.shaded.get() and restored.custom["terminal"] == "#101820"
    appearance.selected.set("System")
    appearance.apply()
    root.update()
    if os.environ.get("PACKET_CAPTURE"):
        subprocess.run(["import", "-window", str(root.winfo_id()), "packet-check/shaded-tabs.png"], check=True)
    appearance.shaded.set(False)
    appearance.apply()
    assert appearance.style.theme_use() == appearance.system_theme

    session.toggle_port()
    os.write(master, b"cmd:")
    until(lambda: session.command_ready)
    errors = []
    packet.messagebox.showerror = lambda *args, **kwargs: errors.append(args)
    editor.text.delete("1.0", "end")
    editor.text.insert("1.0", "VERSION\ncmd:")
    editor.send()
    assert "Line 2" in errors[0][1]
    assert not select.select([master], [], [], .1)[0]
    editor.text.delete("1.0", "end")
    editor.text.insert("1.0", 'LT 1 "data"\nBAD\nVERSION\n')
    editor.continue_on_error.set(True)
    editor.send()
    receive(b'LT 1 "data"\r')
    assert not select.select([master], [], [], .1)[0]
    os.write(master, b"\r\ncmd:\r\n" + b"Monitor output\r\n" * 20)
    receive(b"BAD\r")
    os.write(master, b"\r\nEH?")
    until(lambda: session.batch_rejected == [2])
    assert not select.select([master], [], [], .1)[0]
    os.write(master, b"\r\ncmd:")
    receive(b"VERSION\r")
    os.write(master, b"\r\nKPC9612 VERSION 8.2\r\ncmd:")
    until(lambda: session.batch is None)
    assert "rejected lines 2" in session.terminal.get("1.0", "end")
    packet.COMMAND_TIMEOUT_MS = 240
    assert session.start_batch(packet.parse_command_file("BAD\nVERSION"), continue_on_error=True)
    receive(b"BAD\r")
    os.write(master, b"\r\nEH?")
    until(lambda: session.batch is None)
    assert "No cmd:" in session.batch_status.get()
    assert not select.select([master], [], [], .1)[0]
    session.close()
    os.close(master)
    os.close(slave)
    root.destroy()
print("Command syntax, batch continuation and timeout, editor undo, custom colors and shaded tabs passed.")
