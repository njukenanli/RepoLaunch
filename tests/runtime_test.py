'''
Can be run with pytest -rA

Firstly need to skip different tests for different platform:
# if os == "linux" or "wsl":
# test both LinuxRuntime and AndroidRuntime
# base images: ubuntu:26.04, cimg/android:2026.03.1

# if os == "windows":
# test Windows Runtime
# base images: mcr.microsoft.com/windows/server:ltsc2025

workflow: 
instance={
    "instance_id": "laurent22_joplin-a774",
    "repo": "laurent22/joplin",
    "base_commit": "a774c52fc09371456846be9610680481bd37dc7a"
}
-> session=SetupRuntime.from_base_image(base_image, instance=instance) 
-> res = session.send_command("ls README.md")
-> assert res.metadata.exit_code == 0 ; assert isinstance(res.output, str) and "README.md" in res.output;
-> session.commit(name=f"repolaunch_test}:{platform}_{instance_id}")
-> session.cleanup()

-> session=SetupRuntime.from_launch_image(f"repolaunch_test}:{platform}_{instance_id}", instance_id=instance_id) 
-> res = session.send_command("ls README.md")
-> assert res.metadata.exit_code == 0 ; assert isinstance(res.output, str) and "README.md" in res.output;
-> assert res.apply_patch("""
diff --git a/log.out b/log.out
new file mode 100644
index 0000000..7a754f4
--- /dev/null
+++ b/log.out
@@ -0,0 +1,2 @@
+1
+2
\\ No newline at end of file
""", verbose=True) == True
-> session.cleanup()
'''

import os
import tempfile

import unittest
from unittest.mock import Mock, patch

from docker.errors import ImageNotFound

from launch.core.runtime import (
    CPU_CORES,
    MEM_LIMIT,
    AndroidRuntime,
    CmdOutputMetadata,
    CommandResult,
    SetupRuntime,
)


class FakeAndroidRuntime(AndroidRuntime):
    def __init__(self, container, command_timeout: int = 30):
        self.container = container
        self.command_timeout = command_timeout
        self.platform = "android"
        self.commands = []
        self.cleaned = False
        self.stopped = False

    def send_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.commands.append(command)
        return CommandResult(output="", metadata=CmdOutputMetadata(exit_code=0))

    def cleanup(self, prune_dangling: bool = True) -> None:
        self.cleaned = True


class AndroidRuntimeTest(unittest.TestCase):
    def test_from_launch_image_routes_to_android_runtime(self):
        session = object()

        with patch.object(
            AndroidRuntime,
            "start_runtime_from_launch_image",
            return_value=session,
        ) as start:
            result = SetupRuntime.from_launch_image(
                "cimg/android:2026.03.1-ndk",
                "owner/repo",
                platform="android",
                command_timeout=7,
            )

        self.assertIs(result, session)
        start.assert_called_once_with("cimg/android:2026.03.1-ndk", "owner/repo", 7)

    @patch("launch.core.runtime.docker.from_env")
    def test_android_pull_image_requests_amd64_platform(self, from_env):
        client = Mock()
        client.images.get.side_effect = ImageNotFound("missing")
        from_env.return_value = client

        self.assertTrue(AndroidRuntime.pull_image("cimg/android:2026.03.1"))
        client.images.pull.assert_called_once_with(
            "cimg/android:2026.03.1",
            platform="linux/amd64",
        )

    def test_android_copy_dir_keeps_linux_ownership_fixup(self):
        session = FakeAndroidRuntime(Mock())

        with tempfile.TemporaryDirectory() as tempdir:
            with open(os.path.join(tempdir, "example.txt"), "w") as f:
                f.write("content")

            session.copy_dir_to_container(tempdir, "/dest")

        session.container.put_archive.assert_called_once()
        self.assertEqual(session.commands[-1], 'chown -R root:root "/dest"')

    def test_from_base_image_routes_to_android_runtime(self):
        session = object()
        instance = {
            "instance_id": "owner/repo",
            "repo": "owner/repo",
            "base_commit": "abc123",
        }

        with patch.object(
            AndroidRuntime,
            "start_runtime_from_base_image",
            return_value=session,
        ) as start:
            result = SetupRuntime.from_base_image(
                "cimg/android:2026.03.1-browsers",
                instance,
                platform="android",
                command_timeout=9,
            )

        self.assertIs(result, session)
        start.assert_called_once_with("cimg/android:2026.03.1-browsers", instance, 9)

    @patch.object(FakeAndroidRuntime, "pull_image", return_value=True)
    @patch("launch.core.runtime.docker.from_env")
    def test_start_from_launch_image_uses_android_container_options(
        self,
        from_env,
        _pull_image,
    ):
        client = Mock()
        client.version.return_value = {"Os": "linux"}
        client.containers.run.return_value = Mock()
        from_env.return_value = client

        session = FakeAndroidRuntime.start_runtime_from_launch_image(
            "cimg/android:2026.03.1-ndk",
            "owner/repo",
            command_timeout=11,
        )

        client.containers.run.assert_called_once()
        args, kwargs = client.containers.run.call_args
        expected_tmp = os.path.join(os.getcwd(), "tmp")

        self.assertEqual(args[0], "cimg/android:2026.03.1-ndk")
        self.assertEqual(kwargs["command"], "/bin/bash")
        self.assertEqual(kwargs["working_dir"], "/testbed")
        self.assertEqual(kwargs["platform"], "linux/amd64")
        self.assertEqual(kwargs["user"], "root")
        self.assertEqual(kwargs["cpu_quota"], int(CPU_CORES * 100000))
        self.assertEqual(kwargs["mem_limit"], MEM_LIMIT)
        self.assertEqual(kwargs["extra_hosts"], {"host.docker.internal": "host-gateway"})
        self.assertEqual(
            kwargs["volumes"],
            {
                expected_tmp: {
                    "bind": "/testbed/mnt_tmp",
                    "mode": "rw",
                }
            },
        )
        self.assertEqual(session.platform, "android")
        self.assertEqual(session.command_timeout, 11)
        self.assertEqual(session.mnt_host, expected_tmp)
        self.assertEqual(session.mnt_container, "/testbed/mnt_tmp")

    @patch.object(FakeAndroidRuntime, "pull_image", return_value=True)
    @patch("launch.core.runtime.docker.from_env")
    def test_start_from_base_image_clones_requested_commit(self, from_env, _pull_image):
        client = Mock()
        client.version.return_value = {"OSType": "linux"}
        client.containers.run.return_value = Mock()
        from_env.return_value = client
        instance = {
            "instance_id": "owner/repo",
            "repo": "octo/example",
            "base_commit": "abc123",
        }

        session = FakeAndroidRuntime.start_runtime_from_base_image(
            "cimg/android:2026.03.1-browsers",
            instance,
            command_timeout=13,
        )

        _args, kwargs = client.containers.run.call_args

        self.assertIsNone(kwargs["volumes"])
        self.assertIn("command -v git", session.commands[0])
        self.assertIn("git fetch --depth 1 origin abc123", session.commands[1])
        self.assertIn("git reset --hard abc123", session.commands[1])
        self.assertEqual(session.commands[2], "ls")
        self.assertFalse(session.cleaned)


if __name__ == "__main__":
    unittest.main()
