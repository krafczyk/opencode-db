"""Contract tests for the MkChad-managed launcher deployment action."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
TEMP_ROOT = Path("/tmp/opencode-db-v1")


class DeployInstallerTests(unittest.TestCase):
    """Exercise the installer only against disposable homes and checkouts."""

    def setUp(self) -> None:
        """Create one pinned-checkout-shaped fixture outside live user state."""
        TEMP_ROOT.mkdir(mode=0o700, exist_ok=True)
        self._temporary_directory = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.root = Path(self._temporary_directory.name)
        self.data_home = self.root / "data"
        self.checkout = self.data_home / "mkchad" / "components" / "opencode-db"
        (self.checkout / "bin").mkdir(parents=True)
        shutil.copy2(ROOT / "bin" / "install_opencode_db.sh", self.checkout / "bin")
        shutil.copy2(ROOT / "bin" / "opencode-db", self.checkout / "bin")
        shutil.copytree(
            ROOT / "src" / "opencode_db",
            self.checkout / "src" / "opencode_db",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.recovery = self.root / "recovery"
        self.recovery.mkdir(mode=0o700)
        self.target = self.home / ".local" / "bin" / "opencode-db"
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data_home),
        }

    def tearDown(self) -> None:
        """Remove the isolated test fixture after each deployment scenario."""
        self._temporary_directory.cleanup()

    def test_fresh_install_preflight_and_compliant_check(self) -> None:
        """Preflight is read-only, then install writes the exact checked launcher."""
        preflight = self._run("--check", "--recovery-dir", str(self.recovery))

        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.home / ".local").exists())

        installed = self._run("--recovery-dir", str(self.recovery))
        checked = self._run("--check")

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(
            self.target.read_bytes(),
            (self.checkout / "bin" / "opencode-db").read_bytes(),
        )
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.home / ".local").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / ".local" / "bin").stat().st_mode & 0o777, 0o700)

    def test_replacement_retains_noncompliant_file_and_rerun_is_idempotent(
        self,
    ) -> None:
        """Replace only a regular launcher after retaining its exact old bytes."""
        self.target.parent.mkdir(parents=True, mode=0o700)
        self.target.parent.parent.chmod(0o700)
        self.target.parent.chmod(0o700)
        self.target.write_bytes(b"#!/bin/sh\nexit 99\n")
        self.target.chmod(0o755)

        installed = self._run("--recovery-dir", str(self.recovery))
        rerun = self._run("--recovery-dir", str(self.recovery))

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(
            (self.recovery / "opencode-db").read_bytes(), b"#!/bin/sh\nexit 99\n"
        )
        self.assertEqual((self.recovery / "opencode-db").stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            self.target.read_bytes(),
            (self.checkout / "bin" / "opencode-db").read_bytes(),
        )

    def test_interrupted_backup_allows_exact_retry_only(self) -> None:
        """Resume with an exact private backup and reject ambiguous recovery bytes."""
        self.target.parent.mkdir(parents=True, mode=0o700)
        self.target.parent.parent.chmod(0o700)
        self.target.parent.chmod(0o700)
        self.target.write_bytes(b"old launcher\n")
        self.target.chmod(0o755)
        backup = self.recovery / "opencode-db"
        backup.write_bytes(self.target.read_bytes())
        backup.chmod(0o600)

        retried = self._run("--recovery-dir", str(self.recovery))

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(backup.read_bytes(), b"old launcher\n")

        self.target.write_bytes(b"another old launcher\n")
        refused = self._run("--check", "--recovery-dir", str(self.recovery))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("differs", refused.stderr)

    def test_unsafe_target_refuses_without_touching_referent(self) -> None:
        """Reject a symbolic launcher target without following or replacing it."""
        self.target.parent.mkdir(parents=True, mode=0o700)
        self.target.parent.parent.chmod(0o700)
        self.target.parent.chmod(0o700)
        protected = self.root / "protected"
        protected.write_bytes(b"keep")
        self.target.symlink_to(protected)

        result = self._run("--check", "--recovery-dir", str(self.recovery))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(protected.read_bytes(), b"keep")

    def test_old_python_and_missing_source_refuse_preflight(self) -> None:
        """Require Python 3.11+ and the deployed source package before installation."""
        fake_bin = self.root / "old-python"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
        fake_python.chmod(0o755)
        old_python = self._run(
            "--check",
            "--recovery-dir",
            str(self.recovery),
            environment={
                **self.environment,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            },
        )
        shutil.rmtree(self.checkout / "src" / "opencode_db")
        missing_source = self._run("--check", "--recovery-dir", str(self.recovery))

        self.assertNotEqual(old_python.returncode, 0)
        self.assertIn("Python 3.11", old_python.stderr)
        self.assertNotEqual(missing_source.returncode, 0)
        self.assertIn("source package", missing_source.stderr)
        self.assertFalse(self.target.exists())

    def test_launcher_forwards_arguments_with_only_checkout_source_on_pythonpath(
        self,
    ) -> None:
        """Execute the module with unchanged argv and a checkout-only import path."""
        fake_bin = self.root / "runtime"
        fake_bin.mkdir()
        arguments_file = self.root / "runtime-arguments"
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-P" ] && [ "$2" = "-c" ]; then exit 0; fi\n'
            'printf \'%s\\n\' "$PYTHONPATH" > "$OPENCODE_DB_RUNTIME_ARGUMENTS"\n'
            'printf \'%s\\n\' "$@" >> "$OPENCODE_DB_RUNTIME_ARGUMENTS"\n',
            encoding="ascii",
        )
        fake_python.chmod(0o755)
        environment = {
            **self.environment,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTHONPATH": "ignored",
            "OPENCODE_DB_RUNTIME_ARGUMENTS": str(arguments_file),
        }

        installed = self._run(
            "--recovery-dir", str(self.recovery), environment=environment
        )
        launched = subprocess.run(
            [str(self.target), "cleanup", "status", "--database", "/one path/db"],
            check=False,
            text=True,
            capture_output=True,
            env=environment,
            stdin=subprocess.DEVNULL,
        )

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertEqual(
            arguments_file.read_text(encoding="utf-8").splitlines(),
            [
                str(self.checkout / "src"),
                "-P",
                "-m",
                "opencode_db",
                "cleanup",
                "status",
                "--database",
                "/one path/db",
            ],
        )

    def test_launcher_refuses_a_relative_deployed_data_root(self) -> None:
        """Never resolve the managed checkout relative to the caller directory."""
        installed = self._run("--recovery-dir", str(self.recovery))
        environment = {**self.environment, "XDG_DATA_HOME": "relative-data"}

        launched = subprocess.run(
            [str(self.target), "--help"],
            check=False,
            text=True,
            capture_output=True,
            env=environment,
            stdin=subprocess.DEVNULL,
        )

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertNotEqual(launched.returncode, 0)
        self.assertIn("XDG_DATA_HOME", launched.stderr)

    def test_launcher_ignores_caller_imports_and_keeps_checkout_clean(self) -> None:
        """Use only pinned package bytes without creating bytecode in the checkout."""
        installed = self._run("--recovery-dir", str(self.recovery))
        decoy = self.root / "decoy"
        (decoy / "opencode_db").mkdir(parents=True)
        marker = self.root / "decoy-ran"
        (decoy / "opencode_db" / "__main__.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
            encoding="ascii",
        )
        environment = {
            **self.environment,
            "PYTHONPATH": str(decoy),
            "PYTHONUSERBASE": str(decoy),
        }

        launched = subprocess.run(
            [str(self.target), "--help"],
            cwd=decoy,
            check=False,
            text=True,
            capture_output=True,
            env=environment,
            stdin=subprocess.DEVNULL,
        )

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(list(self.checkout.rglob("__pycache__")))

    def _run(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the fixture installer without executing files from a live checkout."""
        return subprocess.run(
            ["bash", str(self.checkout / "bin" / "install_opencode_db.sh"), *arguments],
            check=False,
            text=True,
            capture_output=True,
            env=environment or self.environment,
            stdin=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    unittest.main()
