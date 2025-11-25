import threading
import time
import queue
import shutil
from io import StringIO

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.console import Group, Console as RichConsole

from . import state, constants, network, commands
from .utils import trim
from .input import start_char_thread

class ChatUI:
    def __init__(self, room, nick, server, f, buf, secret, is_public=False, is_tor=False, shutdown_event=None):
        self.room, self.nick, self.server, self.f, self.secret = room, nick, server, f, secret
        self.buf = buf
        self.is_public = is_public
        self.is_tor = is_tor
        self.shutdown_event = shutdown_event or threading.Event()
        self.layout = Layout()
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="input", size=3),
        )
        self.redraw = True
        self.last_len = len(buf)
        self.last_input = ""
        self.last_terminal_size = (0, 0)
        
        # Performance optimizations
        self._measure_console = None
        self._last_terminal_check = 0
        self._message_height_cache = {}  # Cache for message height calculations

    def _reaper(self):
        """A background thread to remove users who have timed out."""
        while not self.shutdown_event.is_set():
            time.sleep(constants.PING_INTERVAL)
            
            now = time.time()
            # Create a copy of the items to avoid dictionary size changing during iteration
            for user, last_seen in list(state.room_participants.items()):
                if now - last_seen > constants.USER_TIMEOUT:
                    if user == self.nick: continue # Should not happen, but as a safeguard
                    
                    del state.room_participants[user]
                    self.buf.append(("System", Text.from_markup(f"[dim]{user} left (timed out)[/dim]"), False))
                    
                    # Check if the timed-out user was a lottery starter
                    lottery = state.lottery_state.get(self.room)
                    if lottery and lottery["starter"] == user:
                        network.enqueue_sys(self.room, self.nick, "LOTTERY_CANCEL", self.server, self.f)
                        self.buf.append(("System", Text.from_markup(f"[yellow]The lottery was automatically canceled because the starter, [cyan]{user}[/], left.[/]"), False))
                    
                    # Check if the timed-out user was a poll starter
                    poll = state.poll_state.get(self.room)
                    if poll and poll["starter"] == user:
                        network.enqueue_sys(self.room, self.nick, "POLL_CLOSE", self.server, self.f)
                        self.buf.append(("System", Text.from_markup(f"[yellow]The poll was automatically closed because the starter, [cyan]{user}[/], left.[/]"), False))
            
            # Check if room is now empty (only current user left) and auto-cleanup if enabled
            if not self.is_public and len(state.room_participants) <= 1:
                # Room is empty or only current user left - initiate cleanup in background
                threading.Thread(target=self._initiate_auto_cleanup, daemon=True).start()

    def _initiate_auto_cleanup(self):
        """Automatically clean up when room becomes empty."""
        # Add a delay to avoid cleanup during temporary disconnections
        time.sleep(constants.AUTO_CLEANUP_DELAY)
        
        # Re-check if room is still empty after delay
        if not self.is_public and len(state.room_participants) <= 1:
            # Clear local buffer
            self.buf.clear()
            
            # Add auto-cleanup notification
            cleanup_text = Text.from_markup(
                "[bold blue]🤖 Auto-cleanup activated[/]\n\n"
                "The room became empty and chat history has been automatically cleared.\n"
                "[dim]This helps protect privacy by removing old encrypted messages.[/dim]"
            )
            panel = Panel(
                cleanup_text,
                title="[bold blue]🧹 Auto-Cleanup[/]",
                border_style="blue", 
                padding=(1, 2)
            )
            self.buf.append(("System", panel, False))

    def _head(self):
        parts = [
            (" ENCHAT ", "bold cyan"),
            (" CONNECTED ", "bold green"),
            (f" {self.room} ", "white"),
        ]
        if self.is_tor:
            parts.append(("🧅 TOR ", "bold purple"))
        
        parts.extend([
            (f" {self.nick} ", "magenta"),
            (" | " + self.server.replace("https://", ""), "dim")
        ])
        
        return Panel(Text.assemble(*parts), style="blue")

    def _get_message_height(self, renderable, content_width):
        """Efficiently calculate message height with caching."""
        # Create a simple hash key for the renderable
        if hasattr(renderable, '__str__'):
            cache_key = (str(renderable), content_width)
        else:
            cache_key = (repr(renderable), content_width)
        
        if cache_key in self._message_height_cache:
            return self._message_height_cache[cache_key]
        
        # Initialize measure console once and reuse
        if self._measure_console is None or self._measure_console.size.width != content_width:
            self._measure_console = RichConsole(width=content_width, file=StringIO())
        
        # Clear the StringIO buffer
        self._measure_console.file.seek(0)
        self._measure_console.file.truncate(0)
        
        self._measure_console.print(renderable)
        output = self._measure_console.file.getvalue()
        msg_height = output.count('\n')
        
        if msg_height == 0 and output.strip():
            msg_height = 1
            
        # Cache the result, but limit cache size to prevent memory issues
        if len(self._message_height_cache) > 100:
            # Keep only the most recent 50 entries
            keys_to_remove = list(self._message_height_cache.keys())[:-50]
            for key in keys_to_remove:
                del self._message_height_cache[key]
        
        self._message_height_cache[cache_key] = msg_height
        return msg_height

    def _body(self):
        try:
            terminal_size = shutil.get_terminal_size()
            # Header=3, Input=3, Panel-Border=2. Net content height.
            available_height = max(3, terminal_size.lines - 8)
            content_width = terminal_size.columns - 4
        except Exception:
            available_height = 20
            content_width = 80

        renderables = []
        current_height = 0

        # Start from the end and work backwards, but limit how far we look
        # Most terminals show 20-50 lines, so we rarely need to check more than 60 messages
        start_idx = max(0, len(self.buf) - min(60, available_height * 3))
        messages_to_check = list(reversed(self.buf[start_idx:]))

        for msg in messages_to_check:
            sender, content, own = msg[0], msg[1], msg[2]
            is_mention = msg[3] if len(msg) > 3 else False
            
            # Create the specific renderable for the message
            if sender == "System":
                if isinstance(content, Panel):
                    renderable = content
                else:
                    system_text = Text("[SYSTEM] ", style="yellow")
                    if isinstance(content, Text):
                        system_text.append(content)
                    else:
                        system_text.append(Text.from_markup(str(content)))
                    renderable = system_text
            else:
                lab, st = ("You", "green") if own else (sender, "cyan")
                message_text = Text()
                if is_mention:
                    message_text.append(f"{lab}: {content}", style="black on yellow")
                else:
                    message_text.append(f"{lab}: ", style=st)
                    message_text.append(content)
                renderable = message_text
            
            # Use optimized height calculation
            msg_height = self._get_message_height(renderable, content_width)

            if current_height + msg_height > available_height:
                break
            
            renderables.insert(0, renderable)
            current_height += msg_height
                
        return Panel(Group(*renderables), title=f"Messages ({len(self.buf)})", padding=(0, 1))

    def _inp(self):
        entered = "".join(state.current_input)
        txt = Text(f"{self.nick}: ", style="bold green")
        txt.append(entered or "…", style="white")
        txt.append(f"  {len(entered)}/{constants.MAX_MSG_LEN}", style="dim")
        return Panel(Align.left(txt), title="Type message", padding=(0, 1))

    def run(self):
        stop_evt = threading.Event()
        threading.Thread(target=network.listener, args=(self.room, self.nick, self.f, self.server, self.buf, stop_evt, self.shutdown_event), daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()
        start_char_thread()

        # Wait for listener to initialize and receive any existing session keys
        # from other participants before sending our first message. This prevents
        # a race condition where each user generates their own session key.
        time.sleep(2)

        self.buf.append(("System", f"Joined '{self.room}'", False))
        network.enqueue_sys(self.room, self.nick, "joined", self.server, self.f)

        def pinger():
            while not stop_evt.is_set() and not self.shutdown_event.is_set():
                network.enqueue_sys(self.room, self.nick, "ping", self.server, self.f)
                time.sleep(constants.PING_INTERVAL)
        threading.Thread(target=pinger, daemon=True).start()

        with Live(self.layout, refresh_per_second=8, screen=False) as live:
            while not self.shutdown_event.is_set():
                current_time = time.time()
                
                # Check for buffer or input changes
                if len(self.buf) != self.last_len or "".join(state.current_input) != self.last_input:
                    self.redraw = True
                    self.last_len = len(self.buf)
                    self.last_input = "".join(state.current_input)

                # Only check terminal size every 0.5 seconds to reduce system calls
                if current_time - self._last_terminal_check > 0.5:
                    try:
                        current_size = shutil.get_terminal_size()
                        if (current_size.lines, current_size.columns) != self.last_terminal_size:
                            self.last_terminal_size = (current_size.lines, current_size.columns)
                            self.redraw = True
                            # Clear height cache when terminal size changes
                            self._message_height_cache.clear()
                    except:
                        pass
                    self._last_terminal_check = current_time

                if self.redraw:
                    self.layout["header"].update(self._head())
                    self.layout["body"].update(self._body())
                    self.layout["input"].update(self._inp())
                    live.refresh()
                    self.redraw = False

                try:
                    line = state.input_queue.get_nowait()
                except queue.Empty:
                    time.sleep(0.05)
                    continue

                self.redraw = True
                if not line:
                    continue
                
                # Command handling and message sending are now mutually exclusive.
                if line.startswith("/"):
                    res = commands.handle_command(line, self.room, self.nick, self.server, self.f, self.buf, self.secret, self.is_public, self.is_tor)
                    if res == "exit":
                        self.shutdown_event.set()
                        break
                else:
                    # This is a regular message
                    if len(line) > constants.MAX_MSG_LEN:
                        self.buf.append(("System", "❌ Message too long", False, False))
                    else:
                        network.enqueue_msg(self.room, self.nick, line, self.server, self.f)
                        self.buf.append((self.nick, line, True, False))
                
                trim(self.buf)

        # The loop has exited, so we just need to stop the listener thread.
        # The main script (enchat.py) will handle sending the "left" message.
        stop_evt.set()
