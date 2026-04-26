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

import platform as host_platform

import docker
import pytest
from docker.errors import APIError, DockerException, ImageNotFound

from launch.core.runtime import (
    AndroidRuntime,
    SetupRuntime,
)


INSTANCE = {
    "instance_id": "laurent22_joplin-a774",
    "repo": "laurent22/joplin",
    "base_commit": "a774c52fc09371456846be9610680481bd37dc7a",
}
PATCH_CONTENT = """diff --git a/log.out b/log.out\nnew file mode 100644\nindex 0000000..7a754f4\n--- /dev/null\n+++ b/log.out\n@@ -0,0 +1,2 @@\n+1\n+2\n\\ No newline at end of file"""



def supported_integration_platforms() -> set[str]:
    system = host_platform.system().lower()
    if system == "windows":
        return {"windows"}
    if system == "linux":
        return {"linux", "android"}
    return set()


def docker_client_or_skip():
    try:
        client = docker.from_env(timeout=60)
        client.ping()
        return client
    except DockerException as exc:
        pytest.fail(f"Docker is not available: {exc}")


def assert_readme_visible(session) -> None:
    res = session.send_command("ls README.md")
    assert res.metadata.exit_code == 0
    assert isinstance(res.output, str)
    assert "README.md" in res.output.replace("ls README.md", "")


def remove_image_if_present(client, image_ref: str) -> None:
    try:
        client.images.remove(image=image_ref, force=True)
    except (ImageNotFound, APIError):
        pass


def test_android_pull_image_requests_amd64_platform():
    assert AndroidRuntime.pull_image("cimg/android:2026.03.1") is True


@pytest.mark.integration
@pytest.mark.parametrize(
    ("runtime_platform", "base_image"),
    [
        pytest.param("linux", "ubuntu:26.04", id="linux"),
        pytest.param("android", "cimg/android:2026.03.1", id="android"),
        pytest.param("windows", "mcr.microsoft.com/windows/server:ltsc2025", id="windows"),
    ],
)
def test_runtime_integration_workflow(runtime_platform, base_image):
    if runtime_platform not in supported_integration_platforms():
        pytest.skip(f"{runtime_platform} runtime is not supported on this host")

    client = docker_client_or_skip()
    image_repo = "repolaunch_test"
    image_tag = f"{runtime_platform}_{INSTANCE['instance_id']}"
    launch_image = f"{image_repo}:{image_tag}"
    base_session = None
    launch_session = None

    try:
        base_session = SetupRuntime.from_base_image(
            base_image,
            instance=INSTANCE,
            platform=runtime_platform,
        )
        assert_readme_visible(base_session)
        committed_image = base_session.commit(image_name=image_repo, tag=image_tag)
        assert committed_image == launch_image

        assert base_session.apply_patch(PATCH_CONTENT, verbose=True) is True
        rm_res = base_session.send_command("rm log.out")
        assert rm_res.metadata.exit_code == 0, rm_res.output

    finally:
        if base_session is not None:
            base_session.cleanup()

    try:
        launch_session = SetupRuntime.from_launch_image(
            launch_image,
            instance_id=INSTANCE["instance_id"],
            platform=runtime_platform,
        )
        assert_readme_visible(launch_session)
        assert launch_session.apply_patch(PATCH_CONTENT, verbose=True) is True
    finally:
        if launch_session is not None:
            launch_session.cleanup()
        remove_image_if_present(client, launch_image)
