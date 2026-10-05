import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from build_bounded_image import CLEAN_SOURCES, _deleted_paths, _flatten_base, _pin_base, _quote, _runtime_instructions, _verify


class BoundedImageTest(unittest.TestCase):
    def test_runtime_config_and_literal_values(self):
        config = {
            "Env": ['PATH=/bin', 'TEXT=$PATH "quoted" \\tail'], "User": "frappe",
            "WorkingDir": "/home/frappe/frappe-bench", "Entrypoint": ["/entrypoint.sh"],
            "Cmd": None, "Shell": ["/bin/bash", "-c"], "ExposedPorts": {"8000/tcp": {}},
            "Volumes": {"/data": {}}, "Labels": {"revision": "old"}, "StopSignal": "SIGTERM",
            "Healthcheck": {"Test": ["CMD", "echo", "$literal"], "Interval": 1000000000, "Retries": 2},
        }
        lines = _runtime_instructions(config, {"revision": "new"})
        self.assertIn('ENV TEXT="\\$PATH \\"quoted\\" \\\\tail"', lines)
        self.assertIn('LABEL "revision"="new"', lines)
        self.assertIn('ENTRYPOINT ["/entrypoint.sh"]', lines)
        self.assertIn("USER frappe", lines)
        self.assertIn("WORKDIR /home/frappe/frappe-bench", lines)
        self.assertIn("STOPSIGNAL SIGTERM", lines)
        self.assertIn('HEALTHCHECK --interval=1000000000ns --retries=2 CMD ["echo", "$literal"]', lines)
        self.assertFalse(any(line.startswith("CMD ") for line in lines))
        for bad in ({"OnBuild": ["RUN touch /bad"]}, {"Env": ["A=1", "A=2"]},
                    {"User": "root\nRUN bad"}, {"User": '"frappe"'},
                    {"WorkingDir": "/path with spaces"}, {"StopSignal": '"SIGTERM"'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _runtime_instructions(bad, {})
        with self.assertRaises(ValueError):
            _quote("value\nRUN bad")

    def test_build_input_tag_is_created_or_verified_without_overwriting_conflicts(self):
        image_id = "sha256:" + "a" * 64
        tag = "leya/build-input:" + "a" * 64
        with patch("build_bounded_image.subprocess.check_output", return_value="") as images, \
             patch("build_bounded_image.subprocess.run") as run, \
             patch("build_bounded_image._inspect", return_value={"Id": image_id}):
            self.assertEqual(_pin_base(image_id), tag)
            run.assert_called_once_with(["docker", "tag", image_id, tag], check=True)
            run.reset_mock()
            images.return_value = image_id + "\n"
            self.assertEqual(_pin_base(image_id), tag)
            run.assert_not_called()
            images.return_value = "sha256:" + "b" * 64 + "\n"
            with self.assertRaises(ValueError):
                _pin_base(image_id)
            run.assert_not_called()

    def test_deleted_paths_cannot_escape_or_remove_dependencies(self):
        self.assertEqual(_deleted_paths({"flow": {"deleted": ["flow/old.py"]}}), [["flow", "flow/old.py"]])
        for path in ("/etc/passwd", "../other", "a/../../other", "a//b", "a/./b", "node_modules/a", ""):
            with self.subTest(path=path), self.assertRaises(ValueError):
                _deleted_paths({"flow": {"deleted": [path]}})
        with self.assertRaises(ValueError):
            _deleted_paths({"../flow": {"deleted": []}})

    def test_flatten_requires_both_streams_to_succeed_and_removes_anonymous_volumes(self):
        base = {"Id": "sha256:" + "a" * 64, "Os": "linux", "Architecture": "amd64"}
        flat = {**base, "Id": "sha256:" + "b" * 64, "RootFS": {"Layers": ["one"]},
                "Config": {"Labels": {"org.leya.flattened-from": base["Id"]}}}
        for export_code, import_code in ((0, 0), (1, 0), (0, 1)):
            with self.subTest(export=export_code, import_=import_code), \
                 patch("build_bounded_image.subprocess.check_output", side_effect=["", "container-id\n"]), \
                 patch("build_bounded_image.subprocess.Popen") as popen, \
                 patch("build_bounded_image.subprocess.run") as run, \
                 patch("build_bounded_image._inspect", return_value=flat):
                popen.return_value = Mock(wait=Mock(return_value=export_code))
                run.return_value = subprocess.CompletedProcess([], import_code, stdout=flat["Id"] + "\n")
                if export_code or import_code:
                    with self.assertRaises(RuntimeError):
                        _flatten_base(base)
                else:
                    self.assertEqual(_flatten_base(base), ("leya/build-input:flat-" + "a" * 64, flat["Id"]))
                commands = [call.args[0] for call in run.call_args_list]
                self.assertIn(["docker", "rm", "--volumes", "container-id"], commands)
                self.assertEqual(any(command[1] == "tag" for command in commands), not (export_code or import_code))
                popen.return_value.stdout.close.assert_called_once()

    def test_flatten_cache_requires_source_label_and_single_layer(self):
        base = {"Id": "sha256:" + "a" * 64, "Os": "linux", "Architecture": "amd64"}
        flat = {**base, "Id": "sha256:" + "b" * 64, "RootFS": {"Layers": ["one"]},
                "Config": {"Labels": {"org.leya.flattened-from": base["Id"]}}}
        with patch("build_bounded_image.subprocess.check_output", return_value=flat["Id"]), \
             patch("build_bounded_image._inspect", return_value=flat), \
             patch("build_bounded_image.subprocess.Popen") as popen:
            self.assertEqual(_flatten_base(base)[1], flat["Id"])
            flat["RootFS"]["Layers"].append("two")
            with self.assertRaises(ValueError):
                _flatten_base(base)
            flat["RootFS"]["Layers"] = ["one"]
            flat["Config"]["Labels"]["org.leya.flattened-from"] = "wrong"
            with self.assertRaises(ValueError):
                _flatten_base(base)
            popen.assert_not_called()

    def test_git_cleanup_is_limited_to_selected_apps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for app in ("directory", "file", "link", "untouched"):
                (root / app / "node_modules").mkdir(parents=True)
            (root / "directory/.git").mkdir()
            (root / "directory/.git/old-hook").write_text("old")
            (root / "file/.git").write_text("gitdir: elsewhere")
            (root / "untouched/.git").mkdir()
            (root / "untouched/.git/keep").write_text("keep")
            (root / "link/.git").symlink_to(root / "untouched/.git", target_is_directory=True)
            selected = ["directory", "file", "link"]
            subprocess.run([sys.executable, "-c", CLEAN_SOURCES, str(root), json.dumps(selected), "[]"], check=True)
            for app in selected:
                self.assertFalse((root / app / ".git").exists())
                self.assertFalse((root / app / ".git").is_symlink())
                self.assertTrue((root / app / "node_modules").is_dir())
            self.assertEqual((root / "untouched/.git/keep").read_text(), "keep")

    def test_verification_rejects_config_drift_and_layer_growth(self):
        base = {"Config": {"Env": ["PATH=/bin"], "Labels": {"revision": "old"}}, "Os": "linux", "Architecture": "amd64"}
        candidate = copy.deepcopy(base)
        candidate["Config"]["Labels"]["revision"] = "new"
        candidate["RootFS"] = {"Layers": ["one"]}
        _verify(base, candidate, {"revision": "new"})
        candidate["RootFS"]["Layers"] *= 5
        with self.assertRaises(ValueError):
            _verify(base, candidate, {"revision": "new"})
        candidate["RootFS"]["Layers"] = ["one"]
        candidate["Config"]["Env"] = ["PATH=/changed"]
        with self.assertRaises(ValueError):
            _verify(base, candidate, {"revision": "new"})


if __name__ == "__main__":
    unittest.main()
