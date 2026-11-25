import importlib.util
import os
import pty
import select
import tempfile
import time
import tkinter as tk
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "source/package/usr/lib/packet-desk/packet_desk.py"
spec = importlib.util.spec_from_file_location("packet_desk", path)
packet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packet)
assert packet.connect_command("af0wx-7", "w1aw, w5foo-5") == "CONNECT AF0WX-7 VIA W1AW,W5FOO-5"
for bad in ("AF0WX-16", "AF0WX;D", "AF0WX\nDISCONNECT", "ABCDEFG"):
    try:
        packet.connect_command(bad, "")
    except ValueError:
        pass
    else:
        raise AssertionError(bad)
assert packet.encode_line("") == b"\r"
assert [line for _, line, _ in packet.parse_command_file("VERSION\r\nMYCALL\r\n")] == ["VERSION", "MYCALL"]
codec = packet.TerminalDisplay()
assert codec.decode(b"a\r") == "a\n"
assert codec.decode(b"\nb\r\nc\xff") == "b\nc<FF>"

configuration = tempfile.TemporaryDirectory()
packet.profile_file = lambda: Path(configuration.name) / "profiles.json"
root = tk.Tk()
master1, slave1 = pty.openpty()
master2, slave2 = pty.openpty()
app = packet.Application(root, [os.ttyname(slave1), os.ttyname(slave2)])
first, second = app.sessions.values()

def until(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("GUI did not reach expected state")

def transmitted(master, expected):
    assert select.select([master], [], [], 1)[0]
    assert os.read(master, 4096) == expected

def output(session):
    return session.terminal.get("1.0", "end")

with tempfile.TemporaryDirectory() as temporary:
    log = Path(temporary) / "packet.log"
    packet.filedialog.asksaveasfilename = lambda **kwargs: str(log)
    root.update()
    original_bg = first.terminal.cget("background")
    app.appearance.selected.set("Dark")
    app.appearance.save()
    assert first.terminal.cget("background") == packet.Appearance.palettes["Dark"][1]
    assert packet.Appearance(root).selected.get() == "Dark"
    app.appearance.selected.set("System")
    app.appearance.apply()
    assert first.terminal.cget("background") == original_bg
    packet.simpledialog.askstring = lambda *args, **kwargs: "Bench KPC-9612"
    first.baud.set("9600")
    first.flow.set("None")
    first.station.set("AF0WX-7")
    first.via.set("W1AW")
    app.rename_tnc()
    app.save_profile()
    assert app.notebook.tab(first.root, "text") == "Bench KPC-9612"
    profiles = packet.read_profiles()
    profile = profiles["Bench KPC-9612"]
    assert profile == {"port": first.port.get(), "baud": "9600", "flow": "None",
                       "station": "AF0WX-7", "via": "W1AW"}
    restored = app.load_profile("Bench KPC-9612", profile)
    assert restored.radio is None and restored.flow.get() == "None"
    packet.simpledialog.askstring = lambda *args, **kwargs: "Renamed bench radio"
    app.rename_tnc()
    assert packet.read_profiles() == {"Renamed bench radio": profile}
    assert app.notebook.tab(restored.root, "text") == "Renamed bench radio"
    app.close_selected()
    app.notebook.select(first.root)
    first.toggle_port()
    assert app.notebook.tab(first.root, "text") == "Bench KPC-9612"
    second.toggle_port()
    assert not select.select([master1, master2], [], [], .1)[0], "Open must not transmit data"
    first.write_screen("\nClipboard transcript test\n")
    first.terminal.focus_force()
    root.update()
    first.terminal.event_generate("<Control-a>")
    first.terminal.event_generate("<Control-c>")
    root.update()
    assert "Clipboard transcript test" in root.clipboard_get()
    assert first.terminal.cget("state") == "disabled"
    first.terminal.tag_remove("sel", "1.0", "end")
    transcript_menu = next(w for w in first.terminal.children.values() if isinstance(w, tk.Menu))
    transcript_menu.invoke("Copy entire transcript")
    assert "Clipboard transcript test" in root.clipboard_get()
    root.clipboard_clear()
    root.clipboard_append("VERSION")
    first.input.focus_force()
    root.update()
    first.input.event_generate("<Control-v>")
    root.update()
    assert first.line.get() == "VERSION"
    assert not select.select([master1, master2], [], [], .1)[0], "Paste must not transmit data"
    first.input.event_generate("<Control-a>")
    first.input.event_generate("<Control-c>")
    root.update()
    assert root.clipboard_get() == "VERSION"
    first.send_button.invoke()
    transmitted(master1, b"VERSION\r")
    first.toggle_log()
    first.cmd_button.invoke()
    transmitted(master1, b"\x03")
    os.write(master1, b"\r\ncm")
    root.update()
    os.write(master1, b"d:")
    until(lambda: first.command_ready)
    assert not second.command_ready
    first.station.set("af0wx")
    first.via.set("w1aw-7 w5foo")
    first.connect_button.invoke()
    transmitted(master1, b"CONNECT AF0WX VIA W1AW-7,W5FOO\r")
    assert first.disconnect_button.instate(["disabled"])
    first.line.set("Hello")
    first.send_button.invoke()
    transmitted(master1, b"Hello\r")
    os.write(master2, b"\r\nSECOND PORT ONLY\r\n")
    until(lambda: "SECOND PORT ONLY" in output(second))
    assert "SECOND PORT ONLY" not in output(first)
    os.write(master1, b"\r\ncmd:")
    until(lambda: first.command_ready)
    first.send_command("MONITOR ON")
    transmitted(master1, b"MONITOR ON\r")
    os.write(master1, b"\r\ncmd:")
    until(lambda: first.command_ready)
    helper = packet.CommandHelp(root, app.ask_tnc)
    assert len(helper.listing.get_children()) == 172
    helper.model.set("KPC-9612 Plus")
    assert len(helper.listing.get_children()) == 182
    helper.model.set("KAM XL")
    assert len(helper.listing.get_children()) == 240
    helper.filter.set("NONSENSE")
    assert not helper.listing.get_children()
    helper.filter.set("MONITOR")
    helper.listing.selection_set("MONITOR")
    helper.ask_selected()
    transmitted(master1, b"HELP MONITOR\r")
    os.write(master1, b"\r\ncmd:")
    until(lambda: first.command_ready)
    helper.send_help("HELP")
    transmitted(master1, b"HELP\r")
    helper.window.destroy()
    os.write(master1, b"\r\ncmd:")
    until(lambda: first.command_ready)
    first.disconnect_button.invoke()
    transmitted(master1, b"DISCONNE\r")
    os.write(master1, b"\r\ncmd:")
    until(lambda: first.command_ready)
    app.open_editor()
    editor = first.editor
    command_file = Path(temporary) / "settings.txt"
    editor.text.insert("1.0", "# stored settings\nVERSION\n\nMYCALL\n")
    editor.write(command_file)
    assert command_file.read_text().endswith("MYCALL\n")
    packet.filedialog.askopenfilename = lambda **kwargs: str(command_file)
    editor.open_file()
    assert not editor.text.edit_modified()
    app.appearance.selected.set("Warm dark")
    app.appearance.apply()
    assert editor.text.cget("background") == packet.Appearance.palettes["Warm dark"][1]
    editor.send()
    transmitted(master1, b"VERSION\r")
    assert not first.send(b"MANUAL\r", "Manual")
    assert not select.select([master1], [], [], .1)[0], "Second line sent before cmd:"
    os.write(master1, b"\r\nVersion 8.2\r\ncm")
    root.update()
    os.write(master1, b"d:")
    until(lambda: first.batch_status.get().startswith("Sent line 4"))
    transmitted(master1, b"MYCALL\r")
    os.write(master1, b"\r\nMYCALL AF0WX\r\ncmd:")
    until(lambda: first.batch is None)
    assert first.batch_status.get() == "Command file completed"
    assert first.start_batch(packet.parse_command_file("BAD\nNEVER\n"))
    transmitted(master1, b"BAD\r")
    os.write(master1, b"\r\nEH?")
    until(lambda: first.batch is None)
    assert not select.select([master1], [], [], .1)[0]
    os.write(master1, b"\r\ncmd:")
    until(lambda: first.command_ready)
    packet.COMMAND_TIMEOUT_MS = 125
    assert first.start_batch(packet.parse_command_file("SILENT\nNEVER\n"))
    transmitted(master1, b"SILENT\r")
    until(lambda: first.batch is None)
    assert "No cmd:" in first.batch_status.get()
    os.write(master1, b"\r\ncmd:")
    until(lambda: first.command_ready)
    assert first.start_batch(packet.parse_command_file("STOPME\nNEVER\n"))
    transmitted(master1, b"STOPME\r")
    first.stop_batch()
    os.write(master1, b"\r\ncmd:")
    until(lambda: first.command_ready)
    assert not select.select([master1], [], [], .15)[0]
    first.stop_log()
    assert "[Sent] DISCONNE" in log.read_text()
    app.notebook.select(first.root)
    app.close_selected()
    assert first.radio is None and second.radio is not None
    root.update()
    os.close(master2)
    until(lambda: second.radio is None)
    assert "Serial connection lost" in output(second)
    app.close()
print("Tk GUI and two real pseudo-terminal serial sessions passed.")
