"""Linux serial terminal for a Kantronics TNC in TERMINAL mode."""

import argparse
import json
import os
import re
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import serial
from serial.tools import list_ports


PORT_RATES = ("300", "600", "1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200")
FLOW_MODES = ("None", "XON/XOFF", "RTS/CTS")
PROFILE_FIELDS = ("port", "baud", "flow", "station", "via")
HELP_FILE = Path(__file__).resolve().parents[2] / "share/packet-desk/tnc-commands.json"
CALL = r"[A-Z0-9]{1,6}(?:-(?:[0-9]|1[0-5]))?"
PROMPT = re.compile(r"(?:^|\n)cmd:[ \t]*(?=\n|$)", re.IGNORECASE)
BATCH_ERROR = re.compile(r"(?:^|\n)\s*(?:EH\?|\?|Invalid (?:command|parameter|value)\b|Unknown command\b)", re.IGNORECASE)
COMMAND_TIMEOUT_MS = 10000
CONTROL_LABELS = {0x08: "", 0x09: "\t", 0x0A: "\n", 0x0D: "\n", 0x11: "", 0x13: ""}
COMMAND_FILE_HELP = """At the TNC's cmd: prompt, send one TERMINAL command per line. Blank lines and lines starting with # are skipped. A command file is sent one line at a time, with a new cmd: prompt required after each.

KPC-9612 firmware 8.2 examples
    LT 1 Example location text
    LT 2 "data"
    BLT 1 EVERY 00:08:00
    TXDELAY 30/30
    XOFF $13

LT takes a buffer number from 1 to 4 followed by text. Quotes typed in the editor are sent as written. BLT requires its own timer number. For multi-port parameters, / separates the values for port 1 and port 2.

Keep DISPLAY output as a reference. Remove cmd: prompts, link status lines, and display notes such as (300/300 msec) and (CTRL-S). Use the command name on every numbered row you intend to set. Leave out settings you want to keep as they are, including UIFLOOD and UITRACE rows that already show disabled.

Start with VERSION and MYCALL to check the serial link. Send commands that reboot the TNC, change the serial link, or leave command mode separately. Check syntax catches common transcript and formatting problems. Your TNC confirms which commands its firmware accepts."""


def ax25_call(value):
    value = value.strip().upper()
    if not re.fullmatch(CALL, value):
        raise ValueError("Use a callsign such as AF0WX or AF0WX-7.")
    return value


def connect_command(call, via):
    destination = ax25_call(call)
    digipeaters = [ax25_call(item) for item in re.split(r"[ ,]+", via.strip()) if item]
    if len(digipeaters) > 8:
        raise ValueError("Enter up to eight digipeater callsigns.")
    path = " VIA " + ",".join(digipeaters) if digipeaters else ""
    return "CONNECT " + destination + path


def encode_line(value):
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise ValueError("Use printable ASCII text. Use the Cmd button for Ctrl+C.")
    return value.encode("ascii") + b"\r"


class CommandFileError(ValueError):
    def __init__(self, issues):
        self.line_number = issues[0][0]
        messages = [f"Line {number}. {message}" for number, message in issues[:8]]
        if len(issues) > 8:
            messages.append(f"{len(issues) - 8} more lines to check.")
        super().__init__("\n\n".join(messages))


def parse_command_file(text):
    commands = []
    issues = []
    for number, raw in enumerate(re.split(r"\r\n|\n|\r", text), 1):
        line = raw.lstrip(" \t")
        if not line.strip() or line.startswith("#"):
            continue
        # DISPLAY often aligns fields with tabs. Keep spaces and quotes in text payloads.
        line = line.replace("\t", " ")
        try:
            check_command_line(line)
            commands.append((number, line, encode_line(line)))
        except ValueError as error:
            issues.append((number, str(error)))
    if issues:
        raise CommandFileError(issues)
    return commands


def check_command_line(line):
    upper = line.upper()
    if re.match(r"^CMD:", upper) or re.match(r"^\[[^]]+\]", line):
        raise ValueError("Remove the prompt or transcript line.")
    if re.match(r"^(?:[A-Z]/\d+ +)?LINK STATE(?: IS)?:", upper):
        raise ValueError("Remove the link status line.")
    if re.match(r"^\d+(?:\s|$)", line):
        if re.match(r"^[1-4] +(?:EVERY|AFTER)\b", upper):
            raise ValueError("Add BLT before the timer number, or remove this DISPLAY row.")
        raise ValueError('Start the line with its command, for example LT 1 "data".')
    if not re.match(r"^[A-Z0-9]+(?: |$)", upper) or not re.match(r"^[0-9]*[A-Z]", upper):
        raise ValueError("Start the line with a TNC command name.")
    name, _, argument = upper.partition(" ")
    annotated_settings = {"HBAUD", "TXDELAY", "SLOTTIME", "PERSIST", "FRACK", "CHECK", "CMDTIME", "RESPTIME",
                          "XOFF", "XON", "START", "STOP", "COMMAND", "CANLINE", "CANPAC", "DELETE", "PASS",
                          "REDISPLA", "SENDPAC", "STREAMSW"}
    note = re.search(r" +\([^()]*\) *$", line) if name in annotated_settings else None
    if note:
        raise ValueError(f"Remove the trailing DISPLAY note. Use {line[:note.start()]}.")
    if name == "LT" and argument.strip() and not re.match(r"^ *[1-4](?: |% *$|$)", argument):
        raise ValueError('Use a buffer number from 1 to 4, for example LT 1 "data".')


def profile_file():
    config = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(config) / "packet-desk/profiles.json"


def read_profiles():
    try:
        profiles = json.loads(profile_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(profiles, dict):
        raise ValueError("Expected a list of named TNC profiles.")
    for name, profile in profiles.items():
        if (not isinstance(name, str) or not name.strip() or not isinstance(profile, dict)
                or any(not isinstance(profile.get(field), str) for field in PROFILE_FIELDS)
                or profile["baud"] not in PORT_RATES or profile["flow"] not in FLOW_MODES):
            raise ValueError(f"Invalid TNC profile: {name}")
    return profiles


def write_settings(path, settings):
    write_text_file(path, json.dumps(settings, indent=2, sort_keys=True) + "\n")


def write_text_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".profiles-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def bind_clipboard(widget, *, transcript=False, clear_editor=None):
    """Keep the transcript read-only while allowing normal clipboard access."""
    menu = tk.Menu(widget, tearoff=False)
    multiline = isinstance(widget, tk.Text)
    owner = widget.winfo_toplevel()

    def writable():
        if multiline:
            return widget.cget("state") != "disabled"
        return widget.instate(["!disabled", "!readonly"])

    def copy_text(text):
        if text:
            widget.clipboard_clear()
            widget.clipboard_append(text)

    def has_selection():
        return bool(widget.tag_ranges("sel")) if multiline else widget.selection_present()

    def copy_selection(_event=None):
        if not has_selection():
            return "break"
        if multiline:
            copy_text(widget.get("sel.first", "sel.last"))
        else:
            start = widget.index("sel.first")
            end = widget.index("sel.last")
            copy_text(widget.get()[start:end])
        return "break"

    def delete_selection():
        if writable() and has_selection():
            if multiline and widget.cget("undo"):
                widget.edit_separator()
            widget.delete("sel.first", "sel.last")
            if multiline and widget.cget("undo"):
                widget.edit_separator()

    def paste(_event=None):
        if writable():
            widget.event_generate("<<Paste>>")
        return "break"

    def select_all(_event=None):
        if multiline:
            widget.tag_add("sel", "1.0", "end-1c")
        else:
            widget.selection_range(0, "end")
        return "break"

    def show_menu(event):
        widget.focus_set()
        menu.entryconfigure("Copy", state="normal" if has_selection() else "disabled")
        if not transcript:
            menu.entryconfigure("Paste", state="normal" if writable() else "disabled")
            menu.entryconfigure("Delete selection",
                                state="normal" if writable() and has_selection() else "disabled")
        menu.tk_popup(event.x_root, event.y_root)
        # Keep outside clicks working throughout the desktop, including the panel.
        menu.grab_set()
        return "break"

    def dismiss_menu_on_focus_loss(_event):
        if menu.winfo_ismapped() and menu.grab_current() is menu:
            menu.tk.call("::tk::MenuUnpost", str(menu))

    def dismiss_owner_menu(event):
        active_menu = owner.grab_current()
        if not isinstance(active_menu, tk.Menu) or active_menu.master.winfo_toplevel() is not owner:
            return
        if event.type == tk.EventType.Unmap:
            dismiss = event.widget in (owner, active_menu.master)
        else:
            hovered = owner.winfo_containing(*owner.winfo_pointerxy())
            dismiss = hovered is not active_menu and (hovered is None or hovered.winfo_toplevel() is not owner)
        if dismiss:
            # Use Tk's dismissal path to restore focus and release the popup grab.
            owner.tk.call("::tk::MenuUnpost", str(active_menu))

    if not getattr(owner, "clipboard_menu_bound", False):
        owner.bind("<Unmap>", dismiss_owner_menu, add="+")
        owner.bind("<Leave>", dismiss_owner_menu, add="+")
        owner.clipboard_menu_bound = True

    menu.add_command(label="Copy", accelerator="Ctrl+C", command=copy_selection)
    if transcript:
        menu.add_command(label="Copy entire transcript", command=lambda: copy_text(widget.get("1.0", "end-1c")))
    else:
        menu.add_command(label="Paste", accelerator="Ctrl+V", command=paste)
        menu.add_command(label="Delete selection", accelerator="Delete", command=delete_selection)
        widget.bind("<Control-v>", paste)
    menu.add_command(label="Select all", accelerator="Ctrl+A", command=select_all)
    if clear_editor is not None:
        menu.add_separator()
        menu.add_command(label="Clear editor", command=clear_editor)
    widget.bind("<Control-c>", copy_selection)
    widget.bind("<Control-a>", select_all)
    widget.bind("<ButtonRelease-3>", show_menu)
    menu.bind("<FocusOut>", dismiss_menu_on_focus_loss, add="+")
    menu.bind("<Leave>", dismiss_owner_menu, add="+")
    return delete_selection, has_selection


class SerialRadio:
    def __init__(self, port, baud, flow):
        self.device = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0,
            write_timeout=0.3,
            xonxoff=(flow == "XON/XOFF"),
            rtscts=(flow == "RTS/CTS"),
            exclusive=True,
        )

    def read(self):
        return self.device.read(4096)

    def write(self, data):
        if self.device.write(data) != len(data):
            raise serial.SerialTimeoutException("The serial port accepted only part of the line.")

    def close(self):
        self.device.close()


class TerminalDisplay:
    """Make received bytes readable while respecting CRLF across serial reads."""

    def __init__(self):
        self.after_cr = False

    def decode(self, data):
        output = []
        for byte in data:
            if byte == 0x0A and self.after_cr:
                self.after_cr = False
                continue
            self.after_cr = byte == 0x0D
            if byte in CONTROL_LABELS:
                output.append(CONTROL_LABELS[byte])
            elif 0x20 <= byte < 0x7F:
                output.append(chr(byte))
            else:
                output.append(f"<{byte:02X}>")
        return "".join(output)


class PacketDesk:
    def __init__(self, frame, set_title):
        self.root = frame
        self.set_title = set_title
        self.name = ""
        self.profile_name = None
        self.editor = None
        self.radio = None
        self.log = None
        self.display = TerminalDisplay()
        self.reply_tail = ""
        self.command_ready = False
        self.batch = None
        self.batch_job = None
        self.batch_line = 0
        self.batch_rejected = []
        self.batch_continue = False
        self.batch_status = tk.StringVar(value="No batch running")
        self.port = tk.StringVar()
        self.baud = tk.StringVar(value="9600")
        self.flow = tk.StringVar(value="XON/XOFF")
        self.status = tk.StringVar(value="Serial port closed")
        self.station = tk.StringVar()
        self.via = tk.StringVar()
        self.line = tk.StringVar()
        self.build_window()
        self.refresh_ports()
        self.update_controls()
        self.read_job = self.root.after(35, self.read_serial)

    def build_window(self):
        panel = ttk.Frame(self.root, padding=12)
        panel.pack(fill="both", expand=True)

        config = ttk.Frame(panel)
        config.pack(fill="x", pady=(0, 9))
        config.columnconfigure(1, weight=1)
        ttk.Label(config, text="COM / serial port").grid(row=0, column=0, padx=(0, 6))
        self.port_box = ttk.Combobox(config, textvariable=self.port, width=18)
        self.port_box.grid(row=0, column=1, sticky="ew")
        bind_clipboard(self.port_box)
        self.scan_button = ttk.Button(config, text="Refresh", command=self.refresh_ports)
        self.scan_button.grid(row=0, column=2, padx=(5, 10))
        ttk.Label(config, text="Baud").grid(row=0, column=3, padx=(0, 4))
        self.baud_box = ttk.Combobox(config, textvariable=self.baud,
                                     values=PORT_RATES, state="readonly", width=8)
        self.baud_box.grid(row=0, column=4)
        self.flow_box = ttk.Combobox(config, textvariable=self.flow, values=FLOW_MODES,
                                     state="readonly", width=9)
        self.flow_box.grid(row=0, column=5, padx=5)
        self.port_button = ttk.Button(config, text="Open port", command=self.toggle_port)
        self.port_button.grid(row=0, column=6)

        radio_tools = ttk.Frame(panel)
        radio_tools.pack(fill="x", pady=(0, 8))
        ttk.Label(radio_tools, text="Call").grid(row=0, column=0, padx=(0, 4))
        station_entry = ttk.Entry(radio_tools, textvariable=self.station, width=13)
        station_entry.grid(row=0, column=1)
        bind_clipboard(station_entry)
        ttk.Label(radio_tools, text="Via").grid(row=0, column=2, padx=(8, 4))
        via_entry = ttk.Entry(radio_tools, textvariable=self.via, width=14)
        via_entry.grid(row=0, column=3, sticky="ew")
        bind_clipboard(via_entry)
        radio_tools.columnconfigure(3, weight=1)
        self.connect_button = ttk.Button(radio_tools, text="Connect", command=self.connect)
        self.connect_button.grid(row=0, column=4, padx=(6, 3))
        self.disconnect_button = ttk.Button(radio_tools, text="Disconnect", command=lambda: self.send_command("DISCONNE"))
        self.disconnect_button.grid(row=0, column=5)
        self.monitor_button = ttk.Menubutton(radio_tools, text="Monitor")
        monitor_menu = tk.Menu(self.monitor_button, tearoff=False)
        for label, setting in (("On", "ON"), ("Off", "OFF")):
            monitor_menu.add_command(label=label, command=lambda value=setting: self.send_command(f"MONITOR {value}"))
        self.monitor_button.configure(menu=monitor_menu)
        self.monitor_button.grid(row=0, column=6, padx=4)

        self.terminal = ScrolledText(panel, state="disabled", wrap="word", font="TkFixedFont", height=12)
        self.terminal.pack(fill="both", expand=True)
        bind_clipboard(self.terminal, transcript=True)
        self.terminal.tag_configure("status", foreground="#24578a")
        self.terminal.tag_configure("sent", foreground="#8a3c12")

        input_bar = ttk.Frame(panel)
        input_bar.pack(fill="x", pady=(8, 6))
        self.input = ttk.Entry(input_bar, textvariable=self.line, font="TkFixedFont")
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", self.send_text)
        bind_clipboard(self.input)
        self.send_button = ttk.Button(input_bar, text="Send", command=self.send_text)
        self.send_button.pack(side="left", padx=4)
        self.cmd_button = ttk.Button(input_bar, text="Cmd (Ctrl+C)", command=self.enter_command)
        self.cmd_button.pack(side="left")
        self.convers_button = ttk.Button(input_bar, text="Converse", command=lambda: self.send_command("CONVERS"))
        self.convers_button.pack(side="left", padx=(4, 0))

        footer = ttk.Frame(panel)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status).pack(side="left")
        self.log_button = ttk.Button(footer, text="Start log…", command=self.toggle_log)
        self.log_button.pack(side="right")
        ttk.Button(footer, text="Clear screen", command=self.clear_screen).pack(side="right", padx=5)

    def refresh_ports(self):
        paths = sorted(set(port.device for port in list_ports.comports()))
        persistent_paths = sorted(str(item) for item in Path("/dev/serial/by-id").glob("*"))
        board_ports = [str(path) for path in (Path("/dev/serial0"), Path("/dev/serial1")) if path.exists()]
        self.port_box.configure(values=list(dict.fromkeys(persistent_paths + board_ports + paths)))
        if not self.port.get():
            preferred = persistent_paths or [path for path in paths if path.startswith(("/dev/ttyUSB", "/dev/ttyACM"))]
            if preferred:
                self.port.set(preferred[0])

    def update_controls(self):
        opened = self.radio is not None
        manual = opened and self.batch is None
        self.port_button.configure(text="Close port" if opened else "Open port")
        for control in (self.port_box, self.scan_button, self.baud_box, self.flow_box):
            control.state(["disabled"] if opened else ["!disabled"])
        for control in (self.input, self.send_button, self.cmd_button):
            control.state(["!disabled"] if manual else ["disabled"])
        for control in (self.connect_button, self.disconnect_button, self.monitor_button, self.convers_button):
            control.state(["!disabled"] if manual and self.command_ready else ["disabled"])
        if opened:
            mode = "cmd: ready" if self.command_ready else "watch for cmd: or use Cmd (Ctrl+C)"
            if self.batch is not None:
                mode = "sending command file"
            self.status.set(f"{self.port.get()} · {self.baud.get()} baud · {mode}")
        else:
            self.status.set("Serial port closed")

    def toggle_port(self):
        if self.radio is not None:
            self.close_port()
            return
        path = self.port.get().strip()
        if not path:
            messagebox.showinfo("Serial port", "Select a serial port or enter its device path.")
            return
        try:
            self.radio = SerialRadio(path, int(self.baud.get()), self.flow.get())
        except (ValueError, OSError, serial.SerialException) as error:
            messagebox.showerror("Could not open serial port", str(error))
            return
        self.display = TerminalDisplay()
        self.reply_tail = ""
        self.set_title(self.name or Path(path).name)
        self.write_screen(f"\n[Opened {path}. Use Cmd for a TNC already in TERMINAL mode.]\n", "status")
        self.update_controls()
        self.input.focus_set()

    def close_port(self):
        radio, self.radio = self.radio, None
        self.command_ready = False
        self.stop_batch("Serial port closed")
        try:
            if radio is not None:
                radio.close()
        except (OSError, serial.SerialException) as error:
            self.write_screen(f"\n[Close error: {error}]\n", "status")
        self.update_controls()

    def write_screen(self, text, tag=None):
        if not text:
            return
        self.terminal.configure(state="normal")
        at_bottom = self.terminal.yview()[1] >= 0.999
        self.terminal.insert("end", text, tag or ())
        if int(self.terminal.index("end-1c").split(".")[0]) > 12000:
            self.terminal.delete("1.0", "2001.0")
        if at_bottom:
            self.terminal.see("end")
        self.terminal.configure(state="disabled")
        if self.log is not None:
            try:
                self.log.write(text)
                self.log.flush()
            except OSError as error:
                self.stop_log()
                messagebox.showerror("Log write failed", str(error))

    def read_serial(self):
        try:
            if self.radio is not None:
                try:
                    data = self.radio.read()
                except (OSError, serial.SerialException) as error:
                    self.close_port()
                    self.write_screen(f"\n[Serial connection lost: {error}]\n", "status")
                else:
                    received = self.display.decode(data)
                    if received:
                        reply = self.reply_tail + received
                        self.reply_tail = reply[-256:]
                        self.write_screen(received)
                        if (self.batch is not None and not self.command_ready and BATCH_ERROR.search(reply)
                                and self.batch_line not in self.batch_rejected):
                            self.batch_rejected.append(self.batch_line)
                            if self.batch_continue:
                                self.write_screen(f"\n[TNC rejected line {self.batch_line}. Waiting for cmd:.]\n",
                                                  "status")
                            else:
                                self.stop_batch(f"TNC rejected line {self.batch_line}. Check the transcript")
                        if not self.command_ready and PROMPT.search(reply):
                            self.command_ready = True
                            self.update_controls()
                            if self.batch is not None:
                                if self.batch_job is not None:
                                    self.root.after_cancel(self.batch_job)
                                self.batch_job = self.root.after(120, self.advance_batch)
        finally:
            self.read_job = self.root.after(35, self.read_serial)

    def send(self, payload, description, *, from_batch=False):
        if self.radio is None or (self.batch is not None and not from_batch):
            return False
        self.reply_tail = ""
        self.command_ready = False
        self.update_controls()
        try:
            self.radio.write(payload)
        except (OSError, serial.SerialException) as error:
            self.close_port()
            self.write_screen(f"\n[Serial write failed: {error}]\n", "status")
            return False
        self.write_screen(f"\n[Sent] {description}\n", "sent")
        return True

    def start_batch(self, commands, *, continue_on_error=False):
        if not self.command_ready or self.radio is None or self.batch is not None:
            return False
        self.batch = iter(commands)
        self.batch_rejected = []
        self.batch_continue = continue_on_error
        self.advance_batch()
        return True

    def advance_batch(self):
        self.batch_job = None
        next_command = next(self.batch, None)
        if next_command is None:
            if self.batch_rejected:
                lines = ", ".join(map(str, self.batch_rejected))
                self.stop_batch(f"Batch ended with {len(self.batch_rejected)} rejected. See the transcript.",
                                transcript=f"Batch ended with rejected lines {lines}")
            else:
                self.stop_batch("Command file completed")
            return
        number, line, payload = next_command
        self.batch_line = number
        self.batch_status.set(f"Sent line {number}. Waiting for cmd:.")
        if self.send(payload, f"line {number}: {line}", from_batch=True) and self.batch is not None:
            self.batch_job = self.root.after(COMMAND_TIMEOUT_MS, self.stop_batch,
                                             f"No cmd: prompt after line {number}")

    def stop_batch(self, reason="Command file stopped", *, transcript=None):
        if self.batch is None:
            return
        if self.batch_job is not None:
            self.root.after_cancel(self.batch_job)
            self.batch_job = None
        self.batch = None
        self.batch_status.set(reason)
        self.update_controls()
        self.write_screen(f"\n[{transcript or reason}]\n", "status")

    def send_text(self, _event=None):
        value = self.line.get()
        try:
            payload = encode_line(value)
        except ValueError as error:
            messagebox.showerror("Serial input", str(error))
            return
        if self.send(payload, value):
            self.line.set("")

    def enter_command(self):
        self.send(b"\x03", "Ctrl+C")

    def send_command(self, command):
        if self.command_ready:
            self.send(encode_line(command), command)

    def connect(self):
        try:
            command = connect_command(self.station.get(), self.via.get())
        except ValueError as error:
            messagebox.showerror("Station or path", str(error))
            return
        self.send_command(command)

    def clear_screen(self):
        self.terminal.configure(state="normal")
        self.terminal.delete("1.0", "end")
        self.terminal.configure(state="disabled")

    def stop_log(self):
        handle, self.log = self.log, None
        self.log_button.configure(text="Start log…")
        if handle is not None:
            try:
                handle.close()
            except OSError as error:
                messagebox.showerror("Log close failed", str(error))

    def toggle_log(self):
        if self.log is not None:
            self.stop_log()
            return
        filename = filedialog.asksaveasfilename(
            title="Save packet session log", defaultextension=".txt",
            initialfile=datetime.now().strftime("packet-session-%Y%m%d-%H%M.txt"),
            filetypes=[("Text files", "*.txt"), ("All files", "*")],
        )
        if filename:
            try:
                self.log = open(filename, "a", encoding="utf-8")
            except OSError as error:
                messagebox.showerror("Could not open log", str(error))
                return
            self.log_button.configure(text="Stop log")
            self.write_screen(f"\n[Logging from {datetime.now().isoformat(timespec='seconds')}]\n", "status")

    def close(self):
        self.root.after_cancel(self.read_job)
        self.close_port()
        self.stop_log()


class Application:
    def __init__(self, root, ports):
        self.root = root
        self.sessions = {}
        self.root.title("Packet Desk")
        self.root.geometry("960x605")
        self.root.minsize(700, 430)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.appearance = Appearance(root)
        menubar = tk.Menu(root)
        appears = tk.Menu(menubar, tearoff=False)
        for label in self.appearance.choices:
            appears.add_radiobutton(label=label, value=label, variable=self.appearance.selected,
                                    command=self.appearance.save)
        appears.add_separator()
        appears.add_command(label="Customize colors…", command=self.appearance.customize)
        appears.add_checkbutton(label="Shaded tabs", variable=self.appearance.shaded,
                                command=self.appearance.save)
        menubar.add_cascade(label="Appearance", menu=appears)
        self.root.configure(menu=menubar)
        toolbar = ttk.Frame(root, padding=(12, 8))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="New TNC tab", command=self.new_session).pack(side="left")
        ttk.Button(toolbar, text="Close tab", command=self.close_selected).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Rename TNC", command=self.rename_tnc).pack(side="left")
        profiles_button = ttk.Menubutton(toolbar, text="Profiles")
        self.profiles_menu = tk.Menu(profiles_button, tearoff=False, postcommand=self.refresh_profiles)
        profiles_button.configure(menu=self.profiles_menu)
        profiles_button.pack(side="left", padx=6)
        ttk.Button(toolbar, text="Command file…", command=self.open_editor).pack(side="left")
        ttk.Button(toolbar, text="TNC command help", command=self.open_help).pack(side="right")
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        for port in ports or [None]:
            self.new_session(port)
        self.notebook.select(0)
        self.appearance.apply()

    def new_session(self, port=None):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="TNC")
        session = PacketDesk(frame, lambda title: self.notebook.tab(frame, text=title))
        self.sessions[str(frame)] = session
        if port:
            session.port.set(port)
            session.set_title(Path(port).name)
        self.notebook.select(frame)
        self.appearance.apply()
        return session

    def current_session(self):
        return self.sessions[self.notebook.select()]

    def rename_tnc(self):
        session = self.current_session()
        name = self.ask_name("Rename TNC", session)
        if not name or name == session.name:
            return
        if session.profile_name is not None:
            try:
                profiles = read_profiles()
                if name in profiles:
                    raise ValueError(f"A profile named {name} already exists.")
                if session.profile_name in profiles:
                    profiles[name] = profiles.pop(session.profile_name)
                    write_settings(profile_file(), profiles)
                    session.profile_name = name
            except (OSError, ValueError) as error:
                messagebox.showerror("Could not rename profile", str(error), parent=self.root)
                return
        session.name = name
        session.set_title(name)

    def ask_name(self, title, session):
        value = simpledialog.askstring(title, "TNC name", parent=self.root,
                                       initialvalue=session.name or Path(session.port.get()).name)
        return value.strip() if value else ""

    def refresh_profiles(self):
        menu = self.profiles_menu
        menu.delete(0, "end")
        menu.add_command(label="Save current TNC…", command=self.save_profile)
        menu.add_separator()
        try:
            profiles = read_profiles()
        except (OSError, ValueError) as error:
            menu.add_command(label=f"Could not read profiles: {error}", state="disabled")
            return
        if not profiles:
            menu.add_command(label="No saved profiles", state="disabled")
        for name, values in sorted(profiles.items()):
            menu.add_command(label=f"Open {name}", command=lambda n=name, v=values: self.load_profile(n, v))

    def save_profile(self):
        session = self.current_session()
        name = self.ask_name("Save TNC profile", session)
        if not name:
            return
        try:
            profiles = read_profiles()
            if name in profiles and not messagebox.askyesno("Replace profile", f"Update the saved profile {name}?", parent=self.root):
                return
            profiles[name] = {field: getattr(session, field).get() for field in PROFILE_FIELDS}
            write_settings(profile_file(), profiles)
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not save profile", str(error), parent=self.root)
            return
        session.name = name
        session.profile_name = name
        session.set_title(name)
        session.write_screen(f"\n[Saved profile {name}]\n", "status")

    def load_profile(self, name, values):
        session = self.new_session()
        session.name = name
        session.profile_name = name
        session.set_title(name)
        for field in PROFILE_FIELDS:
            getattr(session, field).set(values[field])
        return session

    def open_editor(self):
        session = self.current_session()
        if session.editor is None:
            session.editor = CommandEditor(session)
            self.appearance.apply()
        session.editor.window.deiconify()
        session.editor.window.lift()
        session.editor.text.focus_set()

    def close_selected(self):
        selected = self.notebook.select()
        session = self.sessions[selected]
        if session.editor is not None and not session.editor.confirm_discard():
            return
        del self.sessions[selected]
        session.close()
        self.notebook.forget(selected)
        session.root.destroy()
        if not self.sessions:
            self.new_session()

    def open_help(self):
        CommandHelp(self.root, self.ask_tnc)
        self.appearance.apply()

    def ask_tnc(self, command):
        session = self.current_session()
        if not session.command_ready:
            messagebox.showinfo("TNC help", "Open the port and get to the cmd: prompt before asking the TNC.")
            return False
        return session.send(encode_line(command), command)

    def close(self):
        if any(session.editor is not None and not session.editor.confirm_discard()
               for session in self.sessions.values()):
            return
        for session in self.sessions.values():
            session.close()
        self.root.destroy()


class CommandEditor:
    def __init__(self, session):
        self.session = session
        self.path = None
        self.window = tk.Toplevel(session.root)
        self.window.title("TNC command file")
        self.window.geometry("690x485")
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.continue_on_error = tk.BooleanVar(master=self.window)
        menubar = tk.Menu(self.window)
        edit_menu = tk.Menu(menubar, tearoff=False)
        batch_menu = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        menubar.add_cascade(label="Batch", menu=batch_menu)
        self.window.configure(menu=menubar)
        panel = ttk.Frame(self.window, padding=12)
        panel.pack(fill="both", expand=True)
        toolbar = ttk.Frame(panel)
        toolbar.pack(fill="x", pady=(0, 8))
        for label, action in (("Open…", self.open_file), ("Save", self.save), ("Save as…", self.save_as),
                              ("Send command file", self.send), ("Stop sending", session.stop_batch)):
            ttk.Button(toolbar, text=label, command=action).pack(side="left", padx=(0, 6))
        ttk.Label(panel, text="One command per line. Lines beginning with # are comments.").pack(anchor="w")
        self.text = ScrolledText(panel, font="TkFixedFont", wrap="none", undo=True)
        self.text.pack(fill="both", expand=True, pady=7)
        delete_selection, has_selection = bind_clipboard(self.text, clear_editor=self.clear)
        edit_menu.add_command(label="Delete selection", accelerator="Delete", command=delete_selection)
        edit_menu.add_command(label="Clear editor", command=self.clear)
        edit_menu.configure(postcommand=lambda: edit_menu.entryconfigure(
            "Delete selection", state="normal" if has_selection() else "disabled"))
        batch_menu.add_command(label="Check syntax…", command=self.check)
        batch_menu.add_checkbutton(label="Continue after rejected lines", variable=self.continue_on_error)
        batch_menu.add_command(label="Command file syntax…", command=self.show_syntax)
        ttk.Label(panel, textvariable=session.batch_status).pack(anchor="w")

    def clear(self):
        self.text.edit_separator()
        self.text.delete("1.0", "end")
        self.text.edit_separator()
        self.text.focus_set()

    def confirm_discard(self):
        return not self.text.edit_modified() or messagebox.askyesno(
            "Discard changes?", "Discard the unsaved changes to this command file?", parent=self.window
        )

    def open_file(self):
        if not self.confirm_discard():
            return
        filename = filedialog.askopenfilename(parent=self.window, title="Open TNC command file")
        if not filename:
            return
        path = Path(filename)
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            messagebox.showerror("Could not open file", str(error), parent=self.window)
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.edit_reset()
        self.saved(path)

    def saved(self, path):
        self.path = path
        self.window.title(f"TNC command file — {path.name}")
        self.text.edit_modified(False)

    def save(self):
        if self.path is None:
            self.save_as()
        else:
            self.write(self.path)

    def save_as(self):
        filename = filedialog.asksaveasfilename(parent=self.window, title="Save TNC command file",
                                                 defaultextension=".txt", initialfile="tnc-commands.txt")
        if filename:
            self.write(Path(filename))

    def write(self, path):
        try:
            write_text_file(path.resolve(), self.text.get("1.0", "end-1c"))
        except (OSError, RuntimeError) as error:
            messagebox.showerror("Could not save file", str(error), parent=self.window)
            return
        self.saved(path)

    def read_commands(self):
        try:
            return parse_command_file(self.text.get("1.0", "end-1c"))
        except CommandFileError as error:
            self.text.see(f"{error.line_number}.0")
            messagebox.showerror("Check command file", str(error), parent=self.window)
            return None

    def check(self):
        commands = self.read_commands()
        if commands is not None:
            messagebox.showinfo("Check command file",
                                f"{len(commands)} command lines passed the format check. "
                                "The connected TNC will check the commands when you send them.",
                                parent=self.window)

    def show_syntax(self):
        messagebox.showinfo("Command file syntax", COMMAND_FILE_HELP, parent=self.window)

    def send(self):
        commands = self.read_commands()
        if commands is None:
            return
        if not commands:
            messagebox.showinfo("Command file", "Enter at least one TNC command.", parent=self.window)
        elif not self.session.start_batch(commands, continue_on_error=self.continue_on_error.get()):
            messagebox.showinfo("Command file", "Use the cmd: prompt with no other command file running.",
                                parent=self.window)


class Appearance:
    palettes = {
        "Light": ("#eceff2", "#ffffff", "#242e38", "#637485", "#c8def4", "#144f80", "#844016"),
        "Dark": ("#252525", "#1a1a1a", "#c2d2b7", "#92968d", "#344654", "#74bcf5", "#efb074"),
        "Warm dark": ("#302c29", "#261f1d", "#e8dbce", "#b5a79b", "#604237", "#afe0d0", "#f1c6a0"),
    }
    choices = ("System", *palettes, "Custom")
    labels = {
        "surface": "Window and panel background", "ink": "Labels and control text",
        "muted": "Disabled control text", "border": "Control borders",
        "focus": "Keyboard focus outline", "shine": "Bevel highlight", "shade": "Bevel shadow",
        "field": "Input field and help list background", "field_ink": "Input field and help list text",
        "cursor": "Text cursor", "selection": "Text and list selection background",
        "selection_ink": "Selected text",
        "terminal": "TNC transcript background", "terminal_ink": "Received TNC text",
        "sent": "Sent command text", "status": "Transcript status messages",
        "editor": "Command editor background", "editor_ink": "Command editor text",
        "button": "Buttons and list headers", "button_hover": "Button hover",
        "button_pressed": "Pressed buttons", "arrow": "Control arrows and indicators",
        "menu": "Menu background", "menu_ink": "Menu text", "menu_hover": "Menu highlight",
        "menu_hover_ink": "Highlighted menu text",
        "scroll": "Scrollbar thumb", "scroll_hover": "Scrollbar hover", "trough": "Scrollbar track",
        "tab": "Inactive tab", "tab_hover": "Tab hover", "tab_selected": "Selected tab",
        "tab_ink": "Inactive tab text", "tab_selected_ink": "Selected tab text",
    }

    def __init__(self, root):
        self.root = root
        self.style = ttk.Style(root)
        self.system_theme = root.packet_system_theme if hasattr(root, "packet_system_theme") else self.style.theme_use()
        root.packet_system_theme = self.system_theme
        self.path = profile_file().with_name("appearance.json")
        self.tab_images = {}
        self.color_editor = None
        try:
            settings = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        if isinstance(settings, str):
            settings = {"theme": settings}
        if not isinstance(settings, dict):
            settings = {}
        choice = settings.get("theme")
        self.selected = tk.StringVar(root, value=choice if choice in self.choices else "System")
        self.shaded = tk.BooleanVar(root, value=settings.get("shaded_tabs") is True)
        self.custom = self.colors_for("Light")
        overrides = settings.get("custom")
        if isinstance(overrides, dict):
            for role, value in overrides.items():
                if role in self.labels and self.valid_color(value):
                    self.custom[role] = value

    @staticmethod
    def valid_color(value):
        return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None

    def colors_for(self, choice):
        if choice == "Custom":
            return self.custom.copy()
        surface, field, ink, muted, selection, status, sent = self.palettes.get(choice, self.palettes["Light"])
        colors = dict(surface=surface, ink=ink, muted=muted, selection=selection, status=status, sent=sent,
                      border=muted, focus=status, shine=surface, shade=field, field=field, field_ink=ink,
                      cursor=ink, selection_ink=ink, terminal=field, terminal_ink=ink, editor=field, editor_ink=ink,
                      button=surface, button_hover=selection, button_pressed=field, arrow=ink, menu=surface,
                      menu_ink=ink, menu_hover=selection, menu_hover_ink=ink, scroll=surface,
                      scroll_hover=selection, trough=field, tab=surface, tab_hover=selection, tab_selected=selection,
                      tab_ink=ink, tab_selected_ink=ink)
        if choice == "Dark":
            colors.update(border="#53574f", shine="#444641", button="#3a3a3a", tab="#303030",
                          tab_hover="#414141", tab_selected="#b8131b", tab_selected_ink="#f6efda")
        return colors

    def save(self):
        self.apply()
        try:
            write_settings(self.path, {"theme": self.selected.get(), "shaded_tabs": self.shaded.get(),
                                       "custom": self.custom})
        except OSError as error:
            messagebox.showerror("Could not save appearance", str(error), parent=self.root)

    def customize(self):
        if self.color_editor is None or not self.color_editor.window.winfo_exists():
            self.color_editor = ColorEditor(self)
            self.apply_widgets(self.color_editor.window, self.active_colors())
        else:
            self.color_editor.window.lift()
            self.color_editor.window.focus_set()

    def active_colors(self):
        choice = self.selected.get()
        return None if choice == "System" else self.colors_for(choice)

    def apply(self):
        choice = self.selected.get()
        colors = self.active_colors()
        shaded = self.shaded.get()
        if colors is None and not shaded:
            self.style.theme_use(self.system_theme)
        else:
            name = "packet-" + choice.lower().replace(" ", "-") + ("-shaded" if shaded else "")
            if name not in self.style.theme_names():
                self.style.theme_create(name, parent=self.system_theme if colors is None else "clam")
            self.style.theme_use(name)
            if colors is not None:
                self.style.theme_settings(name, self.theme_settings(colors))
            if shaded:
                tab_colors = colors
                if tab_colors is None:
                    tab_colors = dict(tab="#d9e3ee", tab_hover="#e5effa", tab_selected="#f6f9fe",
                                      border="#8494a8", focus="#df982b")
                self.apply_shaded_tabs(name, tab_colors)
        self.apply_widgets(self.root, colors)

    def theme_settings(self, colors):
        c = colors
        button = {"configure": {"background": c["button"]},
                  "map": {"background": [("pressed", c["button_pressed"]), ("active", c["button_hover"])]}}
        return {
            ".": {"configure": {"background": c["surface"], "foreground": c["ink"], "fieldbackground": c["field"],
                               "bordercolor": c["border"], "troughcolor": c["trough"], "arrowcolor": c["arrow"],
                               "lightcolor": c["shine"], "darkcolor": c["shade"], "focuscolor": c["focus"],
                               "selectbackground": c["selection"], "selectforeground": c["selection_ink"],
                               "insertcolor": c["cursor"]},
                  "map": {"foreground": [("disabled", c["muted"])]}},
            "TButton": button,
            "TMenubutton": button,
            "TEntry": {"configure": {"foreground": c["field_ink"]},
                       "map": {"foreground": [("disabled", c["muted"])]}},
            "TCombobox": {"configure": {"foreground": c["field_ink"]},
                          "map": {"fieldbackground": [("disabled", c["surface"]), ("readonly", c["field"])],
                                  "foreground": [("disabled", c["muted"]), ("readonly", c["field_ink"])]}},
            "TNotebook.Tab": {"configure": {"background": c["tab"], "foreground": c["tab_ink"]},
                              "map": {"background": [("selected", c["tab_selected"]), ("active", c["tab_hover"])],
                                      "foreground": [("selected", c["tab_selected_ink"])]}},
            "TScrollbar": {"configure": {"background": c["scroll"]},
                           "map": {"background": [("active", c["scroll_hover"])]}},
            "Treeview": {"configure": {"background": c["field"], "fieldbackground": c["field"],
                                      "foreground": c["field_ink"]},
                         "map": {"background": [("selected", c["selection"])],
                                 "foreground": [("selected", c["selection_ink"])]}},
            "Treeview.Heading": {"configure": {"background": c["button"], "foreground": c["ink"]}},
        }

    def apply_shaded_tabs(self, theme, colors):
        width, height = 24, 23
        images = self.tab_images.get(theme)
        if images is None:
            images = tuple(tk.PhotoImage(master=self.root, width=width, height=height) for _ in range(3))
            self.tab_images[theme] = images
            normal, hover, selected = images
            element = "PacketDesk.Notebook.tab"
            self.style.element_create(element, "image", normal, ("selected", selected), ("active", hover),
                                      border=(5, 7, 5, 5), sticky="nsew")
            children = self.style.layout("TNotebook.Tab")[0][1]["children"]
            self.style.layout("TNotebook.Tab", [(element, {"sticky": "nsew", "children": children})])
            self.style.configure("TNotebook.Tab", padding=(12, 4))
        for image, role in zip(images, ("tab", "tab_hover", "tab_selected")):
            image.blank()
            rgb = tuple(channel // 257 for channel in self.root.winfo_rgb(colors[role]))
            for row in range(height):
                inset = (3, 2, 1)[row] if row < 3 else 0
                image.put(colors["border"], to=(inset, row, width - inset, row + 1))
                if row == 0 or (row == height - 1 and role != "tab_selected"):
                    continue
                # Shade from a bright upper edge to a darker lower edge.
                blend = 0.31 - 0.43 * row / (height - 1)
                target = 255 if blend >= 0 else 0
                color = "#%02x%02x%02x" % tuple(round(value + (target - value) * abs(blend)) for value in rgb)
                if row == 1 and role == "tab_selected":
                    color = colors["focus"]
                image.put(color, to=(inset + 1, row, width - inset - 1, row + 1))

    def apply_widgets(self, widget, colors):
        options = {}
        if colors is not None:
            c = colors
            if isinstance(widget, tk.Text):
                transcript = "status" in widget.tag_names()
                options = dict(background=c["terminal" if transcript else "editor"],
                               foreground=c["terminal_ink" if transcript else "editor_ink"],
                               insertbackground=c["cursor"], selectbackground=c["selection"],
                               selectforeground=c["selection_ink"], highlightbackground=c["surface"],
                               highlightcolor=c["focus"])
            elif isinstance(widget, tk.Scrollbar) and not isinstance(widget, ttk.Scrollbar):
                options = dict(background=c["scroll"], troughcolor=c["trough"], activebackground=c["scroll_hover"],
                               highlightbackground=c["surface"], highlightcolor=c["focus"])
            elif isinstance(widget, tk.Menu):
                options = dict(background=c["menu"], foreground=c["menu_ink"], activebackground=c["menu_hover"],
                               activeforeground=c["menu_hover_ink"], disabledforeground=c["muted"],
                               selectcolor=c["focus"])
            elif isinstance(widget, (tk.Tk, tk.Toplevel, tk.Canvas)):
                options = dict(background=c["surface"])
        if options and not hasattr(widget, "system_colors"):
            widget.system_colors = {name: widget.cget(name) for name in options}
        widget.configure(**(options or getattr(widget, "system_colors", {})))
        if isinstance(widget, tk.Text) and "status" in widget.tag_names():
            widget.tag_configure("status", foreground=colors["status"] if colors else "#24578a")
            widget.tag_configure("sent", foreground=colors["sent"] if colors else "#8a3c12")
        for child in widget.winfo_children():
            self.apply_widgets(child, colors)


class ColorEditor:
    def __init__(self, appearance):
        self.appearance = appearance
        self.window = tk.Toplevel(appearance.root)
        self.window.title("Customize Packet Desk colors")
        self.window.transient(appearance.root)
        self.window.geometry("590x530")
        self.window.minsize(460, 360)
        panel = ttk.Frame(self.window, padding=12)
        panel.pack(fill="both", expand=True)
        toolbar = ttk.Frame(panel)
        toolbar.pack(fill="x", pady=(0, 10))
        current = appearance.selected.get()
        if current == "System":
            current = "Light"
        self.base = tk.StringVar(self.window, value=current)
        ttk.Label(toolbar, text="Start from").pack(side="left")
        ttk.Combobox(toolbar, textvariable=self.base, values=(*appearance.palettes, "Custom"),
                     state="readonly", width=12).pack(side="left", padx=7)
        ttk.Button(toolbar, text="Load colors", command=self.load).pack(side="left")
        actions = ttk.Frame(panel)
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Button(actions, text="Apply custom colors", command=self.save).pack(side="right")
        ttk.Button(actions, text="Cancel", command=self.window.destroy).pack(side="right", padx=7)
        canvas = tk.Canvas(panel, highlightthickness=0)
        scroll = ttk.Scrollbar(panel, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        rows = ttk.Frame(canvas, padding=(4, 0, 12, 0))
        rows.columnconfigure(0, weight=1)
        content = canvas.create_window((0, 0), window=rows, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(content, width=event.width))
        rows.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        self.window.bind("<Button-4>", lambda _event: canvas.yview_scroll(-3, "units"))
        self.window.bind("<Button-5>", lambda _event: canvas.yview_scroll(3, "units"))
        self.values = {}
        for row, (role, label) in enumerate(appearance.labels.items()):
            ttk.Label(rows, text=label).grid(row=row, column=0, sticky="w", pady=3)
            value = self.values[role] = tk.StringVar(self.window)
            ttk.Entry(rows, textvariable=value, width=10).grid(row=row, column=1, padx=7)
            swatch = tk.Label(rows, width=3, relief="sunken", borderwidth=1)
            swatch.grid(row=row, column=2, padx=(0, 7))
            value.trace_add("write", lambda *_args, var=value, chip=swatch: self.update_swatch(var, chip))
            ttk.Button(rows, text="Choose…", command=lambda var=value, title=label: self.choose(var, title)
                       ).grid(row=row, column=3)
        self.load()

    def load(self):
        colors = self.appearance.colors_for(self.base.get())
        for role, color in colors.items():
            self.values[role].set(color)

    def update_swatch(self, value, swatch):
        if self.appearance.valid_color(value.get()):
            swatch.configure(background=value.get())

    def choose(self, value, title):
        current = value.get() if self.appearance.valid_color(value.get()) else "#ffffff"
        _, color = colorchooser.askcolor(current, title=title, parent=self.window)
        if color:
            value.set(color)

    def save(self):
        colors = {role: value.get().strip() for role, value in self.values.items()}
        for role, value in colors.items():
            if not self.appearance.valid_color(value):
                messagebox.showerror("Invalid color", f"Enter a hex color such as #336699 for "
                                     f"{self.appearance.labels[role]}.", parent=self.window)
                return
        self.appearance.custom = colors
        self.appearance.selected.set("Custom")
        self.window.destroy()
        self.appearance.save()


class CommandHelp:
    def __init__(self, root, ask_tnc):
        reference = json.loads(HELP_FILE.read_text(encoding="utf-8"))
        self.models = reference["models"]
        self.guide = reference["guide"]
        self.ask_tnc = ask_tnc
        self.window = tk.Toplevel(root)
        self.window.title("TNC Command Help")
        self.window.geometry("650x545")
        self.window.minsize(570, 390)
        panel = ttk.Frame(self.window, padding=12)
        panel.pack(fill="both", expand=True)
        selectors = ttk.Frame(panel)
        selectors.pack(fill="x", pady=(0, 8))
        self.model = tk.StringVar(value=next(iter(self.models)))
        self.filter = tk.StringVar()
        ttk.Combobox(selectors, textvariable=self.model, values=list(self.models), state="readonly", width=18).pack(side="left")
        ttk.Label(selectors, text="Find command").pack(side="left", padx=(10, 4))
        search = ttk.Entry(selectors, textvariable=self.filter)
        search.pack(side="left", fill="x", expand=True)
        bind_clipboard(search)
        frame = ttk.Frame(panel)
        frame.pack(fill="both", expand=True)
        self.listing = ttk.Treeview(frame, columns=("page",), selectmode="browse")
        self.listing.heading("#0", text="Command")
        self.listing.heading("page", text="Manual page")
        self.listing.column("page", width=100, stretch=False, anchor="center")
        scrollbar = ttk.Scrollbar(frame, command=self.listing.yview)
        self.listing.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.listing.pack(fill="both", expand=True)
        self.details = tk.StringVar()
        ttk.Label(panel, textvariable=self.details, wraplength=530).pack(anchor="w", pady=8)
        actions = ttk.Frame(panel)
        actions.pack(fill="x")
        ttk.Button(actions, text="Ask TNC about command", command=self.ask_selected).pack(side="left")
        ttk.Button(actions, text="List firmware commands", command=lambda: self.send_help("HELP")).pack(side="left", padx=6)
        ttk.Button(actions, text="Open manual", command=self.open_manual).pack(side="right")
        self.listing.bind("<<TreeviewSelect>>", self.show_selection)
        self.model.trace_add("write", self.refresh)
        self.filter.trace_add("write", self.refresh)
        self.refresh()
        search.focus_set()

    def refresh(self, *_):
        self.listing.delete(*self.listing.get_children())
        query = self.filter.get().strip().upper()
        for command, page in self.models[self.model.get()]["commands"].items():
            if query in command:
                self.listing.insert("", "end", iid=command, text=command, values=(page,))
        self.details.set("Select a command for its manual page or ask the connected TNC at cmd:.")

    def show_selection(self, _event=None):
        selected = self.listing.selection()
        if selected:
            command = selected[0]
            text = self.guide.get(command, "Open the manual for syntax and settings, or ask your TNC for firmware help.")
            self.details.set(f"{command}  •  {text}")

    def ask_selected(self):
        selected = self.listing.selection()
        if selected:
            self.send_help("HELP " + selected[0])

    def send_help(self, command):
        if self.ask_tnc(command):
            self.window.lower()

    def open_manual(self):
        entry = self.models[self.model.get()]
        selected = self.listing.selection()
        anchor = f"#page={entry['commands'][selected[0]]}" if selected else ""
        webbrowser.open(entry["manual"] + anchor)


def main():
    parser = argparse.ArgumentParser(description="Kantronics serial terminal for Linux.")
    parser.add_argument("--port", action="append", help="Preselect a device without opening it. Repeat for multiple tabs.")
    args = parser.parse_args()
    root = tk.Tk()
    Application(root, args.port)
    root.mainloop()


if __name__ == "__main__":
    main()
