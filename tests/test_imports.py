"""Import smoke tests.

The suite passed 97 tests while telegram_bot.py did not compile, because no
test ever imported it. These tests import every top-level module so a syntax
error, a missing dependency, or a renamed symbol fails the build instead of
the running bot.

They must stay cheap: no network, no database, no Ollama, no Telegram.
"""
import importlib
import ast
import io
import os
import re
import sys

import pytest

# Modules that make up the shipped product. Anything added to the repo root
# that is a module should be listed here.
MODULES = [
    "database",
    "saas_manager",
    "multi_provider_ai",
    "multimodal_engine",
    "translator",
    "voice_engine",
    "raven",
    "recon_engine",
    "raven_ui",
    "analytics",
    "content_creator",
    "social_media_manager",
    "trading_engine",
    "energy_manager",
    "badge_verify",
    "dashboard",
    "telegram_bot",
    "webhook_server",
]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    """Every product module must import cleanly.

    This is the test that would have caught the five syntax errors that left
    telegram_bot.py unbuildable.
    """
    importlib.import_module(name)


@pytest.mark.parametrize("name", MODULES)
def test_module_parses(name):
    """Belt-and-braces parse check, independent of import side effects."""
    path = os.path.join(REPO_ROOT, name + ".py")
    with io.open(path, encoding="utf-8") as fh:
        ast.parse(fh.read(), filename=path)


def test_telegram_bot_reaches_run_polling():
    """main() must actually start the bot.

    A previous session's splice left run_polling() stranded inside an
    unrelated function, so main() built the Application and returned without
    polling. Assert the call is a statement of main()'s own body.
    """
    import telegram_bot

    tree = ast.parse(io.open(
        os.path.join(REPO_ROOT, "telegram_bot.py"), encoding="utf-8").read())
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")

    calls = [ast.unparse(s) for s in main_fn.body]
    assert any("run_polling" in c for c in calls), \
        "main() never calls run_polling(); the bot would build and exit"

    # The message handlers must live in main(), not in a callback handler.
    for needed in ("filters.VOICE", "filters.PHOTO", "filters.VIDEO",
                   "filters.Document.ALL", "filters.TEXT"):
        assert any(needed in c for c in calls), \
            f"{needed} handler is not registered by main()"


def test_telegram_bot_menu_callbacks_resolve():
    """Every function button_callback awaits must be defined at module scope.

    32 of the 46 inline-menu functions were written to scratch files but never
    spliced into telegram_bot.py, so every tap would have raised NameError.
    """
    import telegram_bot

    src = io.open(os.path.join(REPO_ROOT, "telegram_bot.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    defined = {n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    cb = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "button_callback")
    referenced = {n.func.id for n in ast.walk(cb)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    missing = sorted(referenced - defined)
    assert not missing, f"button_callback calls undefined functions: {missing}"


def test_telegram_bot_top_level_menus_are_reachable():
    """The three root menus must be routed from some callback."""
    src = io.open(os.path.join(REPO_ROOT, "telegram_bot.py"),
                  encoding="utf-8").read()
    for name in ("show_help_menu", "show_dashboard_menu", "show_settings_menu"):
        assert f"await {name}(" in src, \
            f"{name} is defined but no button routes to it"


def _coroutine_fns(tree):
    """Every async def in the module, including nested ones."""
    return {n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)}


# Calls that legitimately consume a coroutine produced by an inner call.
_CORO_CONSUMERS = frozenset({
    "run", "gather", "create_task", "ensure_future", "wait_for", "shield",
    "run_until_complete", "as_completed", "wait", "to_thread", "run_coroutine_threadsafe",
})


def test_no_coroutine_called_without_await():
    """Calling a coroutine function without awaiting it is a silent bug.

    The async migration of translator/multimodal_engine left six call sites
    unawaited, each of which returned a coroutine object that then blew up
    downstream with an unrelated TypeError - `coroutine object is not
    iterable`, `cannot unpack`, `object is not subscriptable`.

    Only bare-name calls are checked. A method call like
    `page.extract_text()` is resolved against a third-party receiver, so its
    name says nothing about whether it is a coroutine.
    """
    SKIP_MODULES = {"raven"}  # CLI: drives its own loop via asyncio.run

    offenders = []
    for name in MODULES:
        if name in SKIP_MODULES:
            continue
        path = os.path.join(REPO_ROOT, name + ".py")
        with io.open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)

        coros = _coroutine_fns(tree)
        if not coros:
            continue

        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        def consumed(call):
            """True if some enclosing call is an await/coroutine consumer."""
            node = parent.get(call)
            while node is not None:
                if isinstance(node, ast.Await):
                    return True
                if isinstance(node, ast.Call):
                    f = node.func
                    attr = f.attr if isinstance(f, ast.Attribute) else \
                        (f.id if isinstance(f, ast.Name) else "")
                    if attr in _CORO_CONSUMERS:
                        # asyncio.gather(a(), b()) / asyncio.run(x())
                        return True
                node = parent.get(node)
            return False

        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Name):
                continue
            if n.func.id in coros and not consumed(n):
                offenders.append(f"{name}.py:{n.lineno}: {n.func.id}()")

    assert not offenders, "coroutine called without await -> " + ", ".join(offenders)


def test_no_coroutine_passed_to_thread_executor():
    """`asyncio.to_thread` needs a sync callable; given a coroutine it returns one.

    This is how handle_photo/handle_video called the async mm.analyze_image
    and mm.analyze_video: to_thread returned the coroutine object untouched
    and the caller concatenated it into a Telegram message.
    """
    offenders = []
    for name in MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        with io.open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)

        coros = _coroutine_fns(tree)
        if not coros:
            continue

        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            attr = f.attr if isinstance(f, ast.Attribute) else \
                (f.id if isinstance(f, ast.Name) else "")
            if attr != "to_thread" or not n.args:
                continue
            first = n.args[0]
            fname = None
            if isinstance(first, ast.Name):
                fname = first.id
            elif isinstance(first, ast.Attribute):
                fname = first.attr
            if fname in coros:
                offenders.append(f"{name}.py:{n.lineno}: to_thread({fname})")

    assert not offenders, \
        "coroutine handed to asyncio.to_thread -> " + ", ".join(offenders)


def test_declared_dependencies_are_importable():
    """Every third-party module imported outside a try/except must be declared.

    multi_provider_ai.py and webhook_server.py imported aiohttp without adding
    it to requirements.txt, which broke a clean install.

    Imports inside `try: ... except ImportError` are excluded: the code has
    already declared that dependency optional by handling its absence.
    """
    declared = {
        re.split(r"[<>=!~;\[ ]", line.strip().lower())[0]
        for line in io.open(os.path.join(REPO_ROOT, "requirements.txt"),
                            encoding="utf-8")
        if line.strip() and not line.lstrip().startswith("#")
    }

    # Import name -> distribution name, where they differ.
    IMPORT_TO_DIST = {
        "cv2": "opencv-python",
        "PIL": "pillow",
        "sklearn": "scikit-learn",
        "yaml": "pyyaml",
        "bs4": "beautifulsoup4",
        "dotenv": "python-dotenv",
        "docx": "python-docx",
        "speech_recognition": "speechrecognition",
        "telegram": "python-telegram-bot",
        "apscheduler": "apscheduler",
        "serial": "pyserial",
        "Crypto": "pycryptodome",
        "jwt": "pyjwt",
        "dateutil": "python-dateutil",
    }

    local = set(MODULES) | {"setup_desktop", "raven_export", "raven_knowledge"}

    parent = {}
    missing = []
    for name in MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        with io.open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        def optional(node):
            """True if the import sits inside a try that catches ImportError."""
            while node in parent:
                node = parent[node]
                if isinstance(node, ast.Try):
                    for h in node.handlers:
                        exc = ast.unparse(h.type) if h.type else ""
                        if exc in ("ImportError", "ModuleNotFoundError",
                                   "Exception", "BaseException", "OSError"):
                            return True
            return False

        for n in ast.walk(tree):
            tops = []
            if isinstance(n, ast.Import):
                tops = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                tops = [n.module.split(".")[0]]
            for top in tops:
                if top in local or top in sys.stdlib_module_names:
                    continue
                if optional(n):
                    continue
                dist = IMPORT_TO_DIST.get(top, top).lower()
                if dist not in declared and top.lower() not in declared:
                    missing.append(f"{name}.py imports {top} (dist: {dist})")

    assert not missing, \
        "undeclared dependencies: " + "; ".join(sorted(set(missing)))