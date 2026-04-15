"""
startup.py
----------
Windows registry helpers for running the widget automatically at login.

Public API
----------
startup_registered() -> bool
add_to_startup()
remove_from_startup()
"""

import os
import sys

from logging_setup import log

_STARTUP_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "ClaudeUsageWidget"


def _startup_command() -> str:
    """Build the command stored in the registry — uses pythonw to suppress the console."""
    log.debug("Starting _startup_command")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_usage_widget.py")
    result  = f'"{pythonw}" "{script}"'
    log.debug("Finished _startup_command: %s", result)
    return result


def startup_registered() -> bool:
    log.debug("Starting startup_registered")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY) as k:
            val, _ = winreg.QueryValueEx(k, _STARTUP_REG_NAME)
            result = val == _startup_command()
            log.debug("Finished startup_registered: %s", result)
            return result
    except Exception as exc:
        log.debug("startup_registered: not registered (%s)", exc)
        log.debug("Finished startup_registered: False")
        return False


def add_to_startup():
    log.debug("Starting add_to_startup")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                        access=winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, _STARTUP_REG_NAME, 0, winreg.REG_SZ, _startup_command())
    log.debug("Finished add_to_startup")


def remove_from_startup():
    log.debug("Starting remove_from_startup")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                            access=winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _STARTUP_REG_NAME)
    except FileNotFoundError as exc:
        log.debug("remove_from_startup: key not found (%s)", exc)
    log.debug("Finished remove_from_startup")
