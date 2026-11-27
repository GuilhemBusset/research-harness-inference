"""Start accounting before pytest parses options, including in wrappers calling pytest.main."""

import os
import shlex
import sys
from importlib.machinery import PathFinder
from importlib.util import find_spec, module_from_spec, spec_from_file_location


def _legacy_recorder_missing(manager):
    """Check the two sources pytest would try for its ordinary -p option."""
    from importlib.metadata import distributions

    legacy = "implement_loop.loop_pytest"
    if manager.has_plugin(legacy) or legacy in sys.modules:
        return False
    if any(ep.group == "pytest11" and ep.name == legacy for dist in distributions() for ep in dist.entry_points):
        return False
    try:
        return find_spec(legacy) is None
    except ModuleNotFoundError as exc:
        return exc.name in {"implement_loop", legacy}


def _instrument(config_module):
    prepare = config_module._prepareconfig

    def prepare_session(args, plugins=None):
        guard = os.environ.get("LOOP_PYTEST_GUARD")
        original = os.environ.get("PYTEST_ADDOPTS", "")
        if not guard or not original:
            return prepare(args, plugins)
        marker = "@" + guard
        # run_check prefixes the marker. A wrapper may add options before it.
        prefix = shlex.quote(marker) + " "
        if original.startswith(prefix):
            options = original[len(prefix):]
        else:
            words = shlex.split(original)
            if marker not in words:
                return prepare(args, plugins)
            words.remove(marker)
            options = shlex.join(words)

        import tempfile
        fd, start = tempfile.mkstemp(prefix="start-", dir=os.environ["LOOP_REPORT_DIR"])
        os.close(fd)
        old_owner = os.environ.get("LOOP_PYTEST_PID")
        os.environ["PYTEST_ADDOPTS"] = options  # a pytest started by a test is its fixture

        def restore():
            os.environ["PYTEST_ADDOPTS"] = original
            if old_owner is None:
                os.environ.pop("LOOP_PYTEST_PID", None)
            else:
                os.environ["LOOP_PYTEST_PID"] = old_owner

        try:
            name = os.environ["LOOP_PYTEST_RECORDER"]
            if name != "implement_loop.loop_pytest":
                # The verifier binds this directory read-only. The path also
                # identifies its file when another checkout supplies sitecustomize.
                path = os.path.join(os.path.dirname(guard), "recorder.py")
                spec = spec_from_file_location(name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("cannot load the verifier recorder file")
                recorder = module_from_spec(spec)
                spec.loader.exec_module(recorder)
                plugins = [recorder, *(plugins or ())]
                if repair:
                    import pytest

                    class Recovery:
                        @pytest.hookimpl(tryfirst=True)
                        def pytest_load_initial_conftests(self, early_config):
                            # Required plugins have loaded. Check before pytest's
                            # pythonpath ini option changes the module search path.
                            early_config._loop_recovered = _legacy_recorder_missing(early_config.pluginmanager)

                    plugins.append(Recovery())
            config = prepare(args, plugins)
            config._loop_start = start
            config.add_cleanup(restore)
            return config
        except BaseException:
            restore()
            raise

    config_module._prepareconfig = prepare_session


def _pytest_start(event, args):
    # pytest.main sets PYTEST_VERSION before calling _prepareconfig. Wait until
    # then so ordinary Python commands neither import nor require pytest.
    if event == "os.putenv" and args[0] == b"PYTEST_VERSION":
        config = sys.modules.get("_pytest.config")
        if config is not None and not getattr(config, "_loop_instrumented", False):
            vars(config)["_loop_instrumented"] = True
            _instrument(config)


# An inner verifier may inherit a second engine checkout on PYTHONPATH.
previous = getattr(sys.modules[__name__], "_loop_site_chain", ())
repair = False
if not previous and os.environ.get("LOOP_PYTEST_GUARD") and os.environ.pop("LOOP_PYTEST_REPAIR", None):
    added, _, original_path = os.environ["PYTHONPATH"].partition(os.pathsep)
    os.environ["PYTHONPATH"] = original_path
    if added in sys.path:
        sys.path.remove(added)
    repair = True
if os.environ.get("LOOP_PYTEST_GUARD") and not previous:
    sys.addaudithook(_pytest_start)

# Python imports one sitecustomize at startup. Give the existing customization
# its usual module name, skipping both the verifier and its retry symlink.
visited = (*previous, os.path.realpath(__file__))
other = PathFinder.find_spec("sitecustomize", [
    p for p in sys.path if os.path.realpath(os.path.join(p, "sitecustomize.py")) not in visited
])
if other and other.loader:
    module = module_from_spec(other)
    vars(module)["_loop_site_chain"] = visited
    sys.modules["sitecustomize"] = module
    other.loader.exec_module(module)
