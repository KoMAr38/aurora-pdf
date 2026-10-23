"""Aurora PDF - desktop launcher.

Starts the local UI server and opens it in a native window (WebView2 on Windows).

    python main.py [file.pdf]       normal start, optionally opening a file
    python main.py --selftest       check the installation, write a report, exit
    python main.py --browser        run in the default browser instead (development)
    python main.py --serve PORTFILE test server without a window (end-to-end tests)
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading

from api import Api
from core.paths import VERSION, WEB_DIR, data_dir, resource
from core.server import AppServer
from core.session import Session

APP_ID = "Aurora.PDF.Desktop"
UNSAVED = {  # native fallback when the page cannot ask itself
    "sr": "Ima nesačuvanih izmena ili posao još traje. Zatvoriti svejedno?",
    "en": "There are unsaved changes, or a task is still running. Close anyway?",
    "no": "Det finnes ulagrede endringer, eller en jobb pågår fortsatt. Lukke likevel?",
    "de": "Es gibt nicht gespeicherte Änderungen oder eine Aufgabe läuft noch. Trotzdem schließen?",
    "fr": "Des modifications ne sont pas enregistrées, ou une tâche est encore en cours. Fermer quand même ?",
    "it": "Ci sono modifiche non salvate, oppure un'operazione è ancora in corso. Chiudere comunque?",
    "es": "Hay cambios sin guardar o una tarea sigue en curso. ¿Cerrar de todos modos?",
    "pt": "Existem alterações não guardadas ou uma tarefa ainda está em curso. Fechar mesmo assim?",
    "pt-BR": "Há alterações não salvas ou uma tarefa ainda está em andamento. Fechar mesmo assim?",
    "sv": "Det finns osparade ändringar, eller så pågår en uppgift fortfarande. Stäng ändå?",
    "da": "Der er ikke-gemte ændringer, eller en opgave kører stadig. Luk alligevel?",
    "fi": "Muutoksia on tallentamatta tai tehtävä on yhä käynnissä. Suljetaanko silti?",
    "ru": "Есть несохранённые изменения или ещё выполняется задача. Всё равно закрыть?",
    "uk": "Є незбережені зміни або ще виконується завдання. Усе одно закрити?",
    "bg": "Има незаписани промени или задача все още се изпълнява. Да се затвори ли въпреки това?",
    "mk": "Има незачувани промени или некоја задача сè уште трае. Сепак да се затвори?",
    "el": "Υπάρχουν μη αποθηκευμένες αλλαγές ή μια εργασία εκτελείται ακόμα. Κλείσιμο ούτως ή άλλως;",
    "ro": "Există modificări nesalvate sau o activitate este încă în desfășurare. Închideți oricum?",
    "hu": "Nem mentett módosítások vannak, vagy egy feladat még fut. Bezárja mégis?",
    "tr": "Kaydedilmemiş değişiklikler var veya bir görev hâlâ çalışıyor. Yine de kapatılsın mı?",
    "zh-CN": "有未保存的更改，或者仍有任务在运行。仍要关闭吗？",
    "zh-TW": "有未儲存的變更，或仍有工作正在執行。仍要關閉嗎？",
    "ja": "未保存の変更があるか、タスクがまだ実行中です。閉じてもよろしいですか?",
}


def setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(data_dir() / "aurora.log", maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    log = logging.getLogger("aurora")

    # pythonw has no console: what nothing else catches (a window that cannot start) goes to the log
    def uncaught(kind, exc, tb, where="main"):
        log.critical("uncaught error in %s", where, exc_info=(kind, exc, tb))
        sys.__excepthook__(kind, exc, tb)  # and to the console, when there is one (START_MED_KONSOLL.bat)

    sys.excepthook = uncaught
    threading.excepthook = lambda a: uncaught(a.exc_type, a.exc_value, a.exc_traceback,
                                              a.thread.name if a.thread else "?")
    import pymupdf

    pymupdf.set_messages(pylogging=True)  # MuPDF's warnings belong in the log; pythonw has no console
    logging.getLogger("aurora").info("Aurora PDF %s starting (Python %s)", VERSION, sys.version.split()[0])


def start_dark() -> bool:
    """Theme for the first frame, before the page has loaded: the saved choice, else Windows' app colour."""
    from core import settings

    theme = settings.load().get("theme")
    if theme in ("dark", "light"):
        return theme == "dark"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return not winreg.QueryValueEx(key, "AppsUseLightTheme")[0]
    except (ImportError, OSError):
        return True


def set_titlebar(hwnd: int, dark: bool) -> None:
    """Title bar in the page's colours: dark mode flag, then caption and title colours
    (0x00BBGGRR; the colours need Windows 11, Windows 10 only takes the flag)."""
    import ctypes

    caption, text = (0x00120C09, 0x00E5DFDA) if dark else (0x00FFFFFF, 0x003A2014)
    for attr, value in ((20, int(dark)), (35, caption), (36, text)):
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(value)), 4)
    # repaint the frame now; asynchronous because the page asks from the server thread
    ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x4037)  # ASYNCWINDOWPOS FRAMECHANGED NOACTIVATE NOZORDER NOSIZE NOMOVE


def packaged() -> bool:
    """True when running from the Store's MSIX package, which gives the window its taskbar ID itself."""
    try:
        import ctypes

        n = ctypes.c_uint32(0)
        return ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(n), None) != 15700  # APPMODEL_ERROR_NO_PACKAGE
    except (AttributeError, OSError):  # not Windows, or Windows before 8
        return False


def run_window(api: Api, server: AppServer) -> None:
    import webview
    from webview.dom import DOMEventHandler

    if sys.platform.startswith("win") and not packaged():
        try:  # own taskbar group and icon instead of Python's
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass

    dark = start_dark()
    window = webview.create_window(
        "Aurora PDF", server.url, width=1380, height=900, min_size=(1000, 660),
        background_color="#090C12" if dark else "#F3F6FA", text_select=True,
    )
    api.attach_window(window)
    closing = {"force": False}

    def really_quit():
        closing["force"] = True
        window.destroy()

    api._quit = really_quit

    def on_closing():
        if closing["force"]:
            return True

        def ask():  # never block the GUI thread. The page decides: it may hold typed text not sent yet
            try:
                handled = window.evaluate_js("!!(window.Aurora && Aurora.confirmClose())")
            except Exception:
                handled = False
            if handled:
                return
            if not api.s.dirty and not api._jobs.running():
                really_quit()
                return
            from core import settings

            text = UNSAVED.get(settings.load().get("lang"), UNSAVED["en"])
            if window.create_confirmation_dialog("Aurora PDF", text):
                really_quit()

        threading.Thread(target=ask, daemon=True).start()
        return False

    window.events.closing += on_closing

    def on_drop(event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if paths:
            window.evaluate_js(f"window.Aurora && Aurora.dropped({json.dumps(paths)})")

    def on_start(win):
        try:
            win.dom.document.events.drop += DOMEventHandler(on_drop, prevent_default=True)
        except Exception:
            logging.getLogger("aurora").exception("could not enable drag and drop")
        if sys.platform.startswith("win"):
            try:
                import clr  # noqa: F401  (pythonnet, already loaded by pywebview)
                from System.Drawing import Icon  # type: ignore

                win.native.Icon = Icon(str(resource("web", "assets", "aurora.ico")))
                hwnd = win.native.Handle.ToInt32()
                set_titlebar(hwnd, dark)
                api._titlebar = lambda d: set_titlebar(hwnd, d)
            except Exception:
                pass

    webview.start(on_start, window, private_mode=True, debug=bool(os.environ.get("AURORA_DEBUG")))


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        from core.selftest import run

        return run()
    setup_logging()
    test_mode = "--serve" in args
    session = Session()
    api = Api(session, test_mode=test_mode)
    # files to open: plain arguments, except the port file that follows --serve
    api.boot_files = [a for i, a in enumerate(args) if not a.startswith("-") and (i == 0 or args[i - 1] != "--serve")]
    server = AppServer(api, session, WEB_DIR).start()
    if test_mode:  # end-to-end tests: publish the URL and serve until killed
        with open(args[args.index("--serve") + 1], "w", encoding="utf-8") as f:
            f.write(server.url)
        threading.Event().wait()
        return 0
    if "--browser" in args:
        import webbrowser

        webbrowser.open(server.url)
        print("Aurora PDF:", server.url, "(Ctrl+C to stop)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            return 0
    try:
        run_window(api, server)
    finally:
        shutdown(api, session, server)
    return 0


def shutdown(api: Api, session: Session, server: AppServer, wait: float = 60.0) -> None:
    """The window is gone: stop the jobs, then let the document go. A job stops after the page it is on (OCR
    reads one for seconds). One still running after ``wait`` keeps the document to the end of the process:
    after that page it would go on with the document closed under it."""
    if api._jobs.stop_all(wait):
        session.close()
    else:
        logging.getLogger("aurora").warning("a job did not stop in time: its document stays open to the end")
    server.stop()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # the packaged .exe also starts the Word export process (core.pdftools)
    sys.exit(main())
