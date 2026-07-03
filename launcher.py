"""Top-level launcher TUI for pyOBD and Volvo/ADIV tools."""

from __future__ import annotations

import curses
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MenuItem:
    title: str
    description: str
    command: list[str]


REPO_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable

MENU_ITEMS: tuple[MenuItem, ...] = (
    MenuItem(
        "pyOBD legacy GUI",
        "Classic wxPython diagnostic application for ELM-style workflows.",
        [PYTHON, str(REPO_ROOT / "pyobd.py")],
    ),
    MenuItem(
        "ADIV / Volvo P3 TUI",
        "VIDA-style diagnostics, connection setup, and DTC workflow for DiCE.",
        [PYTHON, str(REPO_ROOT / "tui.py")],
    ),
)


class LauncherApp:
    def __init__(self) -> None:
        self.selected = 0
        self.status = "ready"

    def run(self) -> None:
        curses.wrapper(self._run)

    def _run_command(self, stdscr: curses.window, command: list[str]) -> int:
        curses.endwin()
        try:
            result = subprocess.run(command, cwd=REPO_ROOT)
            return result.returncode
        finally:
            curses.reset_prog_mode()
            stdscr.refresh()

    def _draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        title = "pyOBD / ADIV Launcher"
        subtitle = "Choose the tool that matches the task"
        stdscr.addnstr(0, 2, title, max(0, width - 4), curses.A_BOLD)
        stdscr.addnstr(1, 2, subtitle, max(0, width - 4), curses.A_DIM)

        for idx, item in enumerate(MENU_ITEMS):
            row = 4 + idx * 3
            if row >= height - 4:
                break
            active = idx == self.selected
            attr = curses.A_REVERSE if active else curses.A_NORMAL
            stdscr.addnstr(row, 4, item.title, max(0, width - 8), attr | curses.A_BOLD)
            stdscr.addnstr(row + 1, 6, item.description, max(0, width - 10), attr)

        selected = MENU_ITEMS[self.selected]
        detail_top = min(height - 7, 4 + len(MENU_ITEMS) * 3)
        stdscr.addnstr(detail_top, 2, "Command", max(0, width - 4), curses.A_UNDERLINE)
        stdscr.addnstr(detail_top + 1, 4, " ".join(selected.command), max(0, width - 8))
        stdscr.addnstr(detail_top + 3, 2, "Direct commands", max(0, width - 4), curses.A_UNDERLINE)
        stdscr.addnstr(detail_top + 4, 4, "python3 launcher.py", max(0, width - 8))
        stdscr.addnstr(detail_top + 5, 4, "python3 pyobd.py", max(0, width - 8))
        stdscr.addnstr(detail_top + 6, 4, "python3 tui.py", max(0, width - 8))
        stdscr.addnstr(detail_top + 7, 4, "python3 -m volvo_p3.cli tui", max(0, width - 8))

        footer = "Up/Down move  Enter run  q quit"
        stdscr.addnstr(height - 2, 2, f"{self.status}", max(0, width - 4), curses.A_DIM)
        stdscr.addnstr(height - 1, 2, footer, max(0, width - 4), curses.A_DIM)
        stdscr.refresh()

    def _run(self, stdscr: curses.window) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.nodelay(False)
        curses.def_prog_mode()

        while True:
            self._draw(stdscr)
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                break
            if key in (curses.KEY_UP, ord("k")):
                self.selected = (self.selected - 1) % len(MENU_ITEMS)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.selected = (self.selected + 1) % len(MENU_ITEMS)
            elif key in (curses.KEY_ENTER, 10, 13, ord("r"), ord("R")):
                self.status = f"running: {MENU_ITEMS[self.selected].title}"
                stdscr.refresh()
                code = self._run_command(stdscr, MENU_ITEMS[self.selected].command)
                self.status = f"exit code: {code}"


def main() -> int:
    LauncherApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
